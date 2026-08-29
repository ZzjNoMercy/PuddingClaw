"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { FlaskConical, Settings2, X } from "lucide-react";
import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import WorkspacePageHeader from "@/components/layout/WorkspacePageHeader";
import { useApp } from "@/lib/store";
import { getLangSmithSettings, saveLangSmithSettings, testLangSmithConnection, type LangSmithSettings } from "@/lib/evaluationApi";

export default function EvaluationLayout({ children }: { children: React.ReactNode }) {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const pathname = usePathname();
  const [mounted, setMounted] = useState(false);
  const [settings, setSettings] = useState<LangSmithSettings | null>(null);
  const [dialog, setDialog] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => { setMounted(true); getLangSmithSettings().then(setSettings).catch((error) => { setMessage(error instanceof Error ? error.message : "配置加载失败"); setSettings({ enabled: false, endpoint: "https://api.smith.langchain.com", project: "puddingclaw-evaluation", redaction_profile: "default-v1", request_timeout_seconds: 10, max_retries: 2, trace_finalize_timeout_seconds: 5, projection_timeout_seconds: 120, api_key_configured: false }); }); }, []);
  const resize = useCallback((delta: number) => setSidebarWidth((value: number) => Math.max(200, value + delta)), [setSidebarWidth]);
  const persistSettings = async () => {
    if (!settings) throw new Error("配置尚未加载");
    const next = await saveLangSmithSettings({ enabled: settings.enabled, endpoint: settings.endpoint, project: settings.project, workspace_id: settings.workspace_id, redaction_profile: settings.redaction_profile, request_timeout_seconds: settings.request_timeout_seconds, max_retries: settings.max_retries, trace_finalize_timeout_seconds: settings.trace_finalize_timeout_seconds, projection_timeout_seconds: settings.projection_timeout_seconds, api_key: apiKey || undefined });
    setSettings(next); setApiKey("");
    return next;
  };
  const save = async () => {
    try { await persistSettings(); setMessage("配置已保存"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "保存失败"); }
  };
  const test = async () => {
    try { const saved = await persistSettings(); await testLangSmithConnection(); setMessage(saved.enabled ? "连接成功，自动投影已启用" : "连接成功；API Key 有效，自动投影当前未启用"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "连接失败"); }
  };
  return (
    <div className="h-screen app-bg text-gray-900">
      <div className="fixed left-3 top-3 z-[80]"><Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact /></div>
      <div className="flex h-full overflow-hidden">
        <div className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden" style={{ width: sidebarOpen ? sidebarWidth : 0 }}><div style={{ width: sidebarWidth, minWidth: 200 }} className="flex h-full flex-col"><div className="h-11 shrink-0"/><div className="min-h-0 flex-1 overflow-hidden"><Sidebar /></div></div></div>
        {mounted && sidebarOpen && <ResizeHandle onResize={resize} direction="left" />}
        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
            <header className="shrink-0 border-b border-black/[0.06] bg-white/60">
              <div className="workspace-page-width px-5 pb-4 pt-6">
                <WorkspacePageHeader
                  eyebrow="评估工作台"
                  title="评估"
                  description="用版本化评测集和可追踪实验，持续验证智能体的质量、稳定性与回归表现。"
                  actions={
                    <button onClick={() => setDialog(true)} className="flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-600">
                      <span className={`h-2 w-2 rounded-full ${settings?.enabled && settings.api_key_configured && settings.api_key_readable !== false ? "bg-emerald-500" : "bg-gray-300"}`} />LangSmith<Settings2 className="h-3.5 w-3.5" />
                    </button>
                  }
                />
                <div className="mt-5 flex items-center gap-5">
                  <div className="flex items-center gap-2 whitespace-nowrap text-sm font-semibold text-gray-900"><FlaskConical className="h-4 w-4 text-[#002fa7]" />智能体评估</div>
                  <nav className="flex gap-1 text-sm">
                  <Link href="/evaluation/datasets" className={`whitespace-nowrap rounded-lg px-3 py-1.5 ${pathname.startsWith("/evaluation/datasets") ? "bg-[#002fa7] text-white" : "text-gray-500 hover:bg-gray-100"}`}>评测集</Link>
                  <Link href="/evaluation/experiments" className={`whitespace-nowrap rounded-lg px-3 py-1.5 ${pathname.startsWith("/evaluation/experiments") ? "bg-[#002fa7] text-white" : "text-gray-500 hover:bg-gray-100"}`}>评测实验</Link>
                  </nav>
                </div>
              </div>
            </header>
            <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        </main>
      </div>
      {dialog && settings && <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/25 p-4">
        <div className="w-full max-w-lg rounded-2xl bg-white p-5 shadow-xl">
          <div className="mb-4 flex items-center justify-between"><h2 className="font-semibold">LangSmith 评估后端</h2><button onClick={() => setDialog(false)}><X className="h-4 w-4" /></button></div>
          <label className="mb-1 flex items-center gap-2 text-sm"><input type="checkbox" checked={settings.enabled} onChange={(event) => setSettings({ ...settings, enabled: event.target.checked })} />评测完成后自动投影到 LangSmith</label>
          <p className="mb-3 text-xs text-gray-400">关闭自动投影时仍然可以保存 API Key 和测试连接。</p>
          {settings.api_key_readable === false ? <p className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">已保存的 LangSmith API Key 无法解密，请重新录入。其他评估设置仍可编辑。</p> : null}
          <label className="mb-3 block text-xs text-gray-500">服务地址<input value={settings.endpoint} onChange={(event) => setSettings({ ...settings, endpoint: event.target.value })} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-800" /></label>
          <label className="mb-3 block text-xs text-gray-500">项目名称<input value={settings.project} onChange={(event) => setSettings({ ...settings, project: event.target.value })} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-800" /></label>
          <div className="mb-3 grid grid-cols-2 gap-3">
            <label className="block text-xs text-gray-500">单次请求超时（秒）<input type="number" min={1} max={120} value={settings.request_timeout_seconds} onChange={(event) => setSettings({ ...settings, request_timeout_seconds: Number(event.target.value) })} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-800" /></label>
            <label className="block text-xs text-gray-500">最大重试次数<input type="number" min={0} max={5} value={settings.max_retries} onChange={(event) => setSettings({ ...settings, max_retries: Number(event.target.value) })} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-800" /></label>
          </div>
          <label className="mb-3 block text-xs text-gray-500">API Key（{settings.api_key_configured ? `已保存 ${settings.api_key_masked || ""}` : "未配置"}）<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="留空表示保持已保存的 Key" className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-800" /></label>
          {message && <p className="mb-3 text-xs text-gray-600">{message}</p>}
          <div className="flex justify-end gap-2"><button onClick={test} className="rounded-lg border px-3 py-2 text-sm">保存并测试连接</button><button onClick={save} className="rounded-lg bg-[#002fa7] px-3 py-2 text-sm text-white">仅保存</button></div>
        </div>
      </div>}
    </div>
  );
}
