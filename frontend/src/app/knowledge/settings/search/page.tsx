"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Check, Loader2, RefreshCw, Search } from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import { getKnowledgeSearchConfig, getKnowledgeSearchIndexStatus, refreshKnowledgeSearchIndex, updateKnowledgeSearchConfig, type KnowledgeSearchConfig, type KnowledgeSearchIndexStatus } from "@/lib/api";
import { useApp } from "@/lib/store";

export default function KnowledgeSearchSettingsPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [config, setConfig] = useState<KnowledgeSearchConfig | null>(null);
  const [status, setStatus] = useState<KnowledgeSearchIndexStatus | null>(null);
  const [busy, setBusy] = useState<"save" | "refresh" | "rebuild" | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [mounted, setMounted] = useState(false);

  const handleSidebarResize = useCallback((delta: number) => {
    setSidebarWidth((value) => Math.max(200, value + delta));
  }, [setSidebarWidth]);

  async function load() {
    try {
      const [nextConfig, nextStatus] = await Promise.all([getKnowledgeSearchConfig(), getKnowledgeSearchIndexStatus()]);
      setConfig(nextConfig);
      setStatus(nextStatus);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "加载关键词目录设置失败");
    }
  }

  useEffect(() => { setMounted(true); void load(); }, []);

  async function save() {
    if (!config) return;
    setBusy("save"); setError("");
    try { setConfig(await updateKnowledgeSearchConfig(config)); setMessage("搜索范围已保存"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败"); }
    finally { setBusy(null); }
  }

  async function refresh(rebuild: boolean) {
    setBusy(rebuild ? "rebuild" : "refresh"); setError("");
    try { setStatus(await refreshKnowledgeSearchIndex(rebuild)); setMessage("关键词目录已重新扫描"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "更新关键词目录失败"); }
    finally { setBusy(null); }
  }

  return (
    <div className="h-screen app-bg text-gray-900">
      <div className="fixed left-3 top-3 z-[80]"><Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact /></div>
      <div className="flex h-full overflow-hidden">
        <div className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden" style={{ width: sidebarOpen ? sidebarWidth : 0 }}>
          <div style={{ width: sidebarWidth, minWidth: 200 }} className="flex h-full flex-col"><div className="h-11 shrink-0" /><div className="min-h-0 flex-1 overflow-hidden"><Sidebar /></div></div>
        </div>
        {mounted && sidebarOpen ? <ResizeHandle onResize={handleSidebarResize} direction="left" /> : null}
        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto"><div className="workspace-page-container flex flex-col gap-5">
    <div className="mx-auto min-h-full w-full max-w-5xl px-4 py-6 sm:px-7">
      <div className="flex items-start justify-between gap-4">
        <div><Link href="/knowledge/search" className="inline-flex items-center gap-1 text-xs font-semibold text-[#002fa7] hover:underline"><ArrowLeft className="h-3.5 w-3.5" />返回搜索</Link><h1 className="mt-3 text-2xl font-semibold tracking-tight text-gray-950">关键词目录设置</h1><p className="mt-1 text-sm text-gray-500">配置本地 catalog.json 的扫描范围。这里只更新关键词目录，不读取或修改 Milvus。</p></div>
        <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-[#002fa7]/[0.08] text-[#002fa7]"><Search className="h-5 w-5" /></span>
      </div>
      <div className="mt-5"><KnowledgeWorkspaceNav /></div>
      {error ? <div className="mt-5 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">{error}</div> : null}
      {config ? <>
        <section className="mt-5 rounded-2xl border border-black/[0.07] bg-white p-5 shadow-sm">
          <div><h2 className="text-base font-semibold text-gray-950">搜索范围</h2><p className="mt-1 text-xs text-gray-400">配置哪些目录和内容参与搜索。默认排除 Raw、导航文件、隐藏目录和内部运行目录。</p></div>
          <div className="mt-4 divide-y divide-black/[0.06]">
            {config.directories.map((directory) => (
              <label key={directory.id} className="flex items-center gap-3 py-4"><input type="checkbox" checked={directory.enabled} onChange={(event) => setConfig({ ...config, directories: config.directories.map((item) => item.id === directory.id ? { ...item, enabled: event.target.checked } : item) })} className="h-4 w-4 accent-[#002fa7]" /><span className="min-w-0 flex-1"><strong className="block text-sm text-gray-800">{directory.id}</strong><span className="mt-1 block font-mono text-[11px] text-gray-400">{directory.path}</span></span><span className="text-[11px] text-gray-400">{directory.content_types.join(" · ")}</span><span className="min-w-16 text-right text-[11px] text-gray-400">{status?.directories.find((item) => item.id === directory.id)?.status || "—"}</span></label>
            ))}
          </div>
          <div className="mt-3 flex justify-end"><button type="button" onClick={() => void save()} disabled={busy !== null} className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-xs font-semibold text-white hover:bg-[#00227d] disabled:opacity-50">{busy === "save" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}保存范围</button></div>
        </section>
        <section className="mt-5 rounded-2xl border border-black/[0.07] bg-white p-5 shadow-sm">
          <div className="flex items-start justify-between gap-4">
            <div><h2 className="text-base font-semibold text-gray-950">关键词目录</h2><p className="mt-1 text-xs text-gray-400">后台会随文件变化自动增量更新；这里仅用于手动全量校准 catalog.json。语义搜索直接查询已有 Milvus。</p></div>
            <div className="text-right text-xs text-gray-500"><p>{status?.counts.documents || 0} 个文档 · {status?.counts.images || 0} 张图片</p><p className="mt-1 text-[10px] text-gray-400">{status?.generated_at ? new Date(status.generated_at).toLocaleString() : "尚未更新"}</p></div>
          </div>
          <div className="mt-4 flex flex-wrap gap-2"><button type="button" onClick={() => void refresh(false)} disabled={busy !== null} className="inline-flex h-9 items-center gap-2 rounded-xl border border-[#002fa7]/20 px-3 text-xs font-semibold text-[#002fa7] hover:bg-[#002fa7]/[0.05] disabled:opacity-50">{busy === "refresh" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}重新扫描关键词目录</button></div>
        </section>
      </> : <div className="mt-12 flex justify-center text-sm text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在加载…</div>}
      {message ? <p className="mt-4 text-center text-xs font-medium text-emerald-700">{message}</p> : null}
    </div>
          </div></div>
        </main>
      </div>
    </div>
  );
}
