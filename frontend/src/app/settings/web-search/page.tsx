"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  Clock3,
  ExternalLink,
  Globe2,
  KeyRound,
  Loader2,
  Radio,
  RefreshCw,
  Route,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import deepseekLogo from "@lobehub/icons-static-svg/icons/deepseek-color.svg";
import tavilyLogo from "@lobehub/icons-static-svg/icons/tavily-color.svg";
import xaiLogo from "@lobehub/icons-static-svg/icons/xai.svg";

import Navbar from "@/components/layout/Navbar";
import SettingsNavigation from "@/components/settings/SettingsNavigation";
import { useRuntimeProfile } from "@/lib/useRuntimeProfile";
import { useApp } from "@/lib/store";
import {
  deleteWebSearchCredential,
  disableWebSearchProvider,
  enableWebSearchProvider,
  getWebSearchConfig,
  saveWebSearchCredential,
  testWebSearchProvider,
  updateWebSearchProviderOptions,
  updateWebSearchRouting,
  type WebSearchConfig,
  type WebSearchProvider,
  type WebSearchProviderId,
} from "@/lib/webSearchApi";

const PROVIDER_LOGOS: Record<WebSearchProviderId, string | { src: string }> = {
  tavily: tavilyLogo,
  deepseek: deepseekLogo,
  grok: xaiLogo,
};

const STATE_LABELS: Record<string, { label: string; className: string }> = {
  ready: { label: "已就绪", className: "bg-emerald-50 text-emerald-700 ring-emerald-600/15" },
  needs_test: { label: "待测试", className: "bg-amber-50 text-amber-700 ring-amber-600/15" },
  error: { label: "连接异常", className: "bg-rose-50 text-rose-700 ring-rose-600/15" },
  disabled: { label: "未启用", className: "bg-gray-100 text-gray-500 ring-gray-500/10" },
};

function assetUrl(asset: string | { src: string }): string {
  return typeof asset === "string" ? asset : asset.src;
}

function Switch({ checked, onChange, disabled = false, label }: { checked: boolean; onChange: () => void; disabled?: boolean; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={onChange}
      className={`relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${checked ? "bg-[#002fa7]" : "bg-gray-300"}`}
    >
      <span className={`absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${checked ? "translate-x-4" : "translate-x-0"}`} />
    </button>
  );
}

function ProviderMark({ providerId, size = "md" }: { providerId: WebSearchProviderId; size?: "sm" | "md" }) {
  const sizeClass = size === "sm" ? "h-7 w-7 p-1.5" : "h-11 w-11 p-2.5";
  return (
    <span className={`${sizeClass} flex shrink-0 items-center justify-center rounded-xl border border-black/[0.06] bg-white shadow-sm`}>
      <img src={assetUrl(PROVIDER_LOGOS[providerId])} alt="" className="h-full w-full object-contain" />
    </span>
  );
}

function formatTestTime(timestamp?: number): string {
  if (!timestamp) return "尚未测试";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp * 1000));
}

function credentialSourceLabel(provider: WebSearchProvider): string {
  if (provider.credential_source === "provider_registry") return "模型配置";
  if (provider.credential_source === "environment") return "环境变量";
  if (provider.credential_source === "web_search") return "独立 Key";
  return "";
}

export default function WebSearchSettingsPage() {
  const { sidebarOpen, toggleSidebar, sessionId, setSessionId, setWorkspaceView } = useApp();
  const [mounted, setMounted] = useState(false);
  const [config, setConfig] = useState<WebSearchConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState("");
  const [keyDrafts, setKeyDrafts] = useState<Partial<Record<WebSearchProviderId, string>>>({});
  const [editingKeys, setEditingKeys] = useState<Set<WebSearchProviderId>>(() => new Set());
  const [notice, setNotice] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  const runtimeExtensions = useRuntimeProfile();

  const load = useCallback(async () => {
    setLoading(true);
    setNotice(null);
    try {
      const fresh = await getWebSearchConfig();
      setConfig(fresh);
      if (fresh.credential_vault?.readable === false) {
        setNotice({ tone: "error", text: fresh.credential_vault.error || "凭证存储不可读，请重新保存 API Key" });
      }
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "无法加载联网搜索配置" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setMounted(true);
    void load();
  }, [load]);

  const handleReturnToApp = useCallback(() => {
    let targetSessionId = sessionId;
    try {
      targetSessionId = sessionStorage.getItem("puddingclaw_session_id") || sessionId;
    } catch {
      // Keep the current in-memory session.
    }
    setWorkspaceView("chat");
    if (targetSessionId !== sessionId) setSessionId(targetSessionId);
  }, [sessionId, setSessionId, setWorkspaceView]);

  const providersById = useMemo(
    () => new Map((config?.providers || []).map((provider) => [provider.id, provider])),
    [config],
  );

  const runAction = useCallback(async <T,>(id: string, operation: () => Promise<T>, successText: string) => {
    setAction(id);
    setNotice(null);
    try {
      const result = await operation();
      setNotice({ tone: "success", text: successText });
      return result;
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "操作失败" });
      return null;
    } finally {
      setAction("");
    }
  }, []);

  const updateRouting = useCallback(async (update: Parameters<typeof updateWebSearchRouting>[0], successText = "路由设置已保存") => {
    const next = await runAction("routing", () => updateWebSearchRouting(update), successText);
    if (next) setConfig(next);
  }, [runAction]);

  const moveProvider = useCallback(async (scope: "domestic" | "global", providerId: WebSearchProviderId, direction: -1 | 1) => {
    if (!config) return;
    const order = [...config.routing[scope]];
    const index = order.indexOf(providerId);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= order.length) return;
    [order[index], order[nextIndex]] = [order[nextIndex], order[index]];
    await updateRouting({ [scope]: order }, `${scope === "domestic" ? "国内" : "全球"}路由已更新`);
  }, [config, updateRouting]);

  const saveKey = useCallback(async (providerId: WebSearchProviderId) => {
    const key = keyDrafts[providerId]?.trim();
    if (!key) {
      setNotice({ tone: "error", text: "请输入 API Key" });
      return;
    }
    const next = await runAction(`key:${providerId}`, () => saveWebSearchCredential(providerId, key), "API Key 已安全保存，请先测试连接");
    if (next) {
      setConfig(next);
      setKeyDrafts((current) => ({ ...current, [providerId]: "" }));
      setEditingKeys((current) => {
        const updated = new Set(current);
        updated.delete(providerId);
        return updated;
      });
    }
  }, [keyDrafts, runAction]);

  const testProvider = useCallback(async (providerId: WebSearchProviderId) => {
    const result = await runAction(`test:${providerId}`, () => testWebSearchProvider(providerId), "连接和搜索能力验证通过");
    if (result) await load();
  }, [load, runAction]);

  const toggleProvider = useCallback(async (provider: WebSearchProvider) => {
    const operation = provider.enabled
      ? () => disableWebSearchProvider(provider.id)
      : () => enableWebSearchProvider(provider.id);
    const next = await runAction(
      `toggle:${provider.id}`,
      operation,
      provider.enabled ? `${provider.name} 已停用，凭证和测试状态仍保留` : `${provider.name} 已启用`,
    );
    if (next) setConfig(next);
  }, [runAction]);

  const deleteKey = useCallback(async (provider: WebSearchProvider) => {
    if (!window.confirm(`删除 ${provider.name} 的独立 API Key？该供应商会同时停用。`)) return;
    const next = await runAction(`delete:${provider.id}`, () => deleteWebSearchCredential(provider.id), "独立 API Key 已删除");
    if (next) setConfig(next);
  }, [runAction]);

  const updateProviderOptions = useCallback(async (providerId: WebSearchProviderId, options: Record<string, unknown>) => {
    const next = await runAction(`options:${providerId}`, () => updateWebSearchProviderOptions(providerId, options), "供应商选项已保存");
    if (next) setConfig(next);
  }, [runAction]);

  return (
    <div className="h-screen app-bg">
      <div className="fixed left-3 top-3 z-[80]">
        <Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact />
      </div>

      <div className="flex h-full overflow-hidden">
        <div className="workspace-sidebar-shell shrink-0 overflow-hidden panel-transition" style={{ width: !mounted || sidebarOpen ? 208 : 0 }}>
          <div className="flex h-full w-52 flex-col">
            <div className="h-11 shrink-0" />
            <SettingsNavigation active="webSearch" extensions={runtimeExtensions} onReturnToApp={handleReturnToApp} />
          </div>
        </div>

        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          {loading && !config ? (
            <div className="flex flex-1 items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-gray-400" /></div>
          ) : config ? (
            <div className="flex-1 overflow-y-auto px-5 pb-10 pt-6 sm:px-8">
              <div className="mx-auto max-w-6xl space-y-6">
                <header className="border-b border-black/[0.06] pb-6">
                  <div className="flex items-start gap-3.5">
                    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.08] text-[#002fa7]">
                      <Globe2 className="h-5 w-5" />
                    </span>
                    <div>
                      <h1 className="text-[22px] font-semibold tracking-tight text-gray-900">联网搜索</h1>
                      <p className="mt-1 max-w-2xl text-[12px] leading-5 text-gray-500">
                        为 Agent 配置公开互联网搜索。国内默认使用 DeepSeek，全球默认使用 Grok；Tavily 参与通用回退与交叉验证。
                      </p>
                    </div>
                  </div>
                </header>

                {notice ? (
                  <div className={`flex items-start gap-2.5 rounded-xl border px-3.5 py-3 text-[12px] ${notice.tone === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-rose-200 bg-rose-50 text-rose-800"}`}>
                    {notice.tone === "success" ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /> : <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />}
                    <span>{notice.text}</span>
                  </div>
                ) : null}

                <section className="grid gap-3 sm:grid-cols-3">
                  <SummaryCard icon={CheckCircle2} label="已启用供应商" value={`${config.ready_providers.length} / 3`} detail={config.ready_providers.length ? config.ready_providers.map((id) => providersById.get(id)?.name || id).join("、") : "尚未启用"} />
                  <SummaryCard icon={Route} label="国内公网" value={providersById.get(config.routing.domestic[0])?.name || "—"} detail={config.routing.domestic.slice(1).map((id) => providersById.get(id)?.name || id).join(" → ")} />
                  <SummaryCard icon={Globe2} label="全球公网" value={providersById.get(config.routing.global[0])?.name || "—"} detail={config.routing.global.slice(1).map((id) => providersById.get(id)?.name || id).join(" → ")} />
                </section>

                <section>
                  <div className="mb-3 flex items-end justify-between gap-4">
                    <div>
                      <h2 className="text-[15px] font-semibold text-gray-900">搜索供应商</h2>
                      <p className="mt-1 text-[11px] text-gray-500">保存 Key 后先测试连接，通过后再启用；测试与启用互不触发。当前三家均复用内置依赖。</p>
                    </div>
                  </div>
                  <div className="grid gap-4 lg:grid-cols-3">
                    {config.providers.map((provider) => (
                      <ProviderCard
                        key={provider.id}
                        provider={provider}
                        busy={action.includes(provider.id)}
                        keyDraft={keyDrafts[provider.id] || ""}
                        editingKey={editingKeys.has(provider.id) || !provider.credential_configured}
                        onEditKey={() => setEditingKeys((current) => new Set(current).add(provider.id))}
                        onCancelKey={() => setEditingKeys((current) => { const next = new Set(current); next.delete(provider.id); return next; })}
                        onKeyChange={(value) => setKeyDrafts((current) => ({ ...current, [provider.id]: value }))}
                        onSaveKey={() => void saveKey(provider.id)}
                        onDeleteKey={() => void deleteKey(provider)}
                        onTest={() => void testProvider(provider.id)}
                        onToggle={() => void toggleProvider(provider)}
                        onOptions={(options) => void updateProviderOptions(provider.id, options)}
                      />
                    ))}
                  </div>
                </section>

                <section className="rounded-2xl border border-black/[0.055] bg-white p-5 shadow-sm">
                  <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex items-start gap-3">
                      <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-[#002fa7]/[0.07] text-[#002fa7]"><Route className="h-4 w-4" /></span>
                      <div><h2 className="text-[14px] font-semibold text-gray-900">搜索路由</h2><p className="mt-1 text-[11px] leading-4 text-gray-500">Agent 选择搜索范围，后端按这里的顺序执行和回退。X Search 始终由 Grok 执行。</p></div>
                    </div>
                    <div className="flex rounded-lg bg-gray-100 p-0.5 text-[11px]">
                      {(["global", "domestic"] as const).map((scope) => (
                        <button key={scope} type="button" onClick={() => void updateRouting({ default_scope: scope })} className={`rounded-md px-3 py-1.5 font-medium transition ${config.default_scope === scope ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-800"}`}>
                          默认{scope === "global" ? "全球" : "国内"}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="mt-5 grid gap-4 lg:grid-cols-2">
                    <RouteColumn title="国内公网" description="中文站点与国内服务优先" scope="domestic" order={config.routing.domestic} providers={providersById} busy={action === "routing"} onMove={moveProvider} />
                    <RouteColumn title="全球公网" description="全球网页优先使用 Grok" scope="global" order={config.routing.global} providers={providersById} busy={action === "routing"} onMove={moveProvider} />
                  </div>

                  <div className="mt-4 grid gap-3 border-t border-black/[0.055] pt-4 sm:grid-cols-3">
                    <OptionRow title="失败时自动回退" description={`最多尝试 ${config.routing.max_provider_attempts} 家供应商`} checked={config.routing.fallback_enabled} onChange={() => void updateRouting({ fallback_enabled: !config.routing.fallback_enabled })} disabled={action === "routing"} />
                    <SelectOptionRow
                      title="最大尝试数"
                      description="控制延迟与重复计费"
                      value={config.routing.max_provider_attempts}
                      disabled={action === "routing"}
                      onChange={(value) => void updateRouting({ max_provider_attempts: value })}
                    />
                    <OptionRow title="允许多源交叉验证" description="仅在用户明确要求核实时使用第二来源" checked={config.routing.cross_check_enabled} onChange={() => void updateRouting({ cross_check_enabled: !config.routing.cross_check_enabled })} disabled={action === "routing"} />
                  </div>
                </section>

                <section className="flex items-start gap-3 rounded-2xl border border-blue-100 bg-[#f7f9ff] px-4 py-3.5 text-[11px] leading-5 text-gray-600">
                  <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-[#002fa7]" />
                  <p>搜索问题会发送给所选第三方，但不会携带完整对话、附件、数据库结果或内部 Wiki 原文。API Key 只保存在仓库外的 CredentialStore，页面、日志和 Agent 上下文只显示掩码。</p>
                </section>
              </div>
            </div>
          ) : (
            <div className="flex flex-1 items-center justify-center px-6">
              <div className="w-full max-w-md rounded-2xl border border-rose-100 bg-white p-6 text-center shadow-sm">
                <span className="mx-auto flex h-11 w-11 items-center justify-center rounded-2xl bg-rose-50 text-rose-600"><AlertCircle className="h-5 w-5" /></span>
                <h1 className="mt-4 text-[15px] font-semibold text-gray-900">无法加载联网搜索设置</h1>
                <p className="mt-1.5 text-[11px] leading-5 text-gray-500">{notice?.text || "请确认后端服务已启动，然后重试。"}</p>
                <button type="button" onClick={() => void load()} className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-[#002fa7] px-4 py-2 text-[11px] font-semibold text-white hover:bg-[#001f7a]"><RefreshCw className="h-3.5 w-3.5" />重新加载</button>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function SummaryCard({ icon: Icon, label, value, detail }: { icon: React.ElementType; label: string; value: string; detail: string }) {
  return (
    <div className="rounded-2xl border border-black/[0.055] bg-white px-4 py-3.5 shadow-sm">
      <div className="flex items-center gap-2 text-[10px] font-medium text-gray-400"><Icon className="h-3.5 w-3.5 text-[#002fa7]" />{label}</div>
      <div className="mt-2 truncate text-[16px] font-semibold text-gray-900">{value}</div>
      <div className="mt-0.5 truncate text-[10px] text-gray-400">{detail || "无回退"}</div>
    </div>
  );
}

function ProviderCard({
  provider,
  busy,
  keyDraft,
  editingKey,
  onEditKey,
  onCancelKey,
  onKeyChange,
  onSaveKey,
  onDeleteKey,
  onTest,
  onToggle,
  onOptions,
}: {
  provider: WebSearchProvider;
  busy: boolean;
  keyDraft: string;
  editingKey: boolean;
  onEditKey: () => void;
  onCancelKey: () => void;
  onKeyChange: (value: string) => void;
  onSaveKey: () => void;
  onDeleteKey: () => void;
  onTest: () => void;
  onToggle: () => void;
  onOptions: (options: Record<string, unknown>) => void;
}) {
  const state = STATE_LABELS[provider.enabled ? "ready" : provider.state] || STATE_LABELS.disabled;
  const inherited = provider.credential_source === "provider_registry";

  return (
    <article className={`flex min-h-[430px] flex-col rounded-2xl border bg-white p-4 shadow-sm transition ${provider.enabled ? "border-[#002fa7]/20 ring-1 ring-[#002fa7]/[0.04]" : "border-black/[0.06]"}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <ProviderMark providerId={provider.id} />
          <div className="min-w-0"><h3 className="truncate text-[14px] font-semibold text-gray-900">{provider.name}</h3><p className="mt-0.5 truncate font-mono text-[10px] text-gray-400">{provider.model || "Search REST API"}</p></div>
        </div>
        <span className={`shrink-0 rounded-full px-2 py-1 text-[9px] font-semibold ring-1 ring-inset ${state.className}`}>{state.label}</span>
      </div>

      <p className="mt-4 min-h-10 text-[11px] leading-5 text-gray-500">{provider.description}</p>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {provider.id === "grok" ? <><CapabilityTag label="全球公网" active={provider.options.web_search_enabled !== false} /><CapabilityTag label="X Search" active={provider.options.x_search_enabled !== false} /></> : null}
        {provider.id === "deepseek" ? <CapabilityTag label="国内公网" active /> : null}
        {provider.id === "tavily" ? <><CapabilityTag label="全球公网" active /><CapabilityTag label="国内可用" active /></> : null}
      </div>

      <div className="mt-4 space-y-2.5 border-t border-black/[0.055] pt-4">
        <div className="flex items-center justify-between gap-3 text-[10px]">
          <span className="flex shrink-0 items-center gap-1.5 text-gray-400"><KeyRound className="h-3.5 w-3.5" />API Key</span>
          {provider.credential_configured ? (
            <span className="flex min-w-0 items-center justify-end gap-1.5">
              <span className="truncate font-mono font-medium text-emerald-700">{provider.api_key_masked}</span>
              <span className={`shrink-0 rounded-md px-1.5 py-0.5 font-medium ${provider.credential_readable === false ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700"}`}>{provider.credential_readable === false ? "需重新录入" : credentialSourceLabel(provider)}</span>
            </span>
          ) : <span className="text-amber-700">未配置</span>}
        </div>
        <div className="flex items-center justify-between gap-3 text-[10px]"><span className="flex items-center gap-1.5 text-gray-400"><Radio className="h-3.5 w-3.5" />依赖</span><span className="font-medium text-gray-600">已内置，无额外安装</span></div>
        <div className="flex items-center justify-between gap-3 text-[10px]"><span className="flex items-center gap-1.5 text-gray-400"><Clock3 className="h-3.5 w-3.5" />最近测试</span><span className="text-gray-500">{formatTestTime(provider.last_test?.tested_at)}{provider.last_test?.success ? ` · ${provider.last_test.latency_ms}ms` : ""}</span></div>
      </div>

      {provider.id === "grok" ? (
        <div className="mt-3 grid grid-cols-2 gap-2">
          <MiniOption label="Web Search" checked={provider.options.web_search_enabled !== false} disabled={busy} onChange={() => onOptions({ web_search_enabled: provider.options.web_search_enabled === false })} />
          <MiniOption label="X Search" checked={provider.options.x_search_enabled !== false} disabled={busy} onChange={() => onOptions({ x_search_enabled: provider.options.x_search_enabled === false })} />
        </div>
      ) : null}

      {provider.id === "tavily" ? (
        <label className="mt-3 flex items-center justify-between gap-3 rounded-lg bg-gray-50 px-2.5 py-2 text-[10px] text-gray-500">
          搜索深度
          <select value={provider.options.search_depth || "basic"} disabled={busy} onChange={(event) => onOptions({ search_depth: event.target.value })} className="rounded-md border border-gray-200 bg-white px-2 py-1 text-[10px] font-medium text-gray-700 outline-none focus:border-[#002fa7]">
            <option value="basic">Basic</option><option value="advanced">Advanced</option><option value="fast">Fast</option>
          </select>
        </label>
      ) : null}

      <div className="mt-auto pt-4">
        {editingKey ? (
          <form className="space-y-2" onSubmit={(event) => { event.preventDefault(); onSaveKey(); }}>
            <input type="password" value={keyDraft} onChange={(event) => onKeyChange(event.target.value)} placeholder={provider.credential_configured ? "输入新的独立 API Key" : "输入 API Key"} className="h-9 w-full rounded-lg border border-gray-200 bg-gray-50 px-3 text-[11px] text-gray-800 outline-none transition focus:border-[#002fa7] focus:bg-white" />
            <div className="flex gap-2">
              <button type="submit" disabled={busy || !keyDraft.trim()} className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-[#002fa7] px-3 py-2 text-[10px] font-semibold text-white transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45">{busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}保存 Key</button>
              {provider.credential_configured ? <button type="button" onClick={onCancelKey} className="rounded-lg border border-gray-200 px-3 py-2 text-[10px] font-medium text-gray-500 hover:bg-gray-50">取消</button> : null}
            </div>
          </form>
        ) : (
          <div className="flex gap-2">
            <button type="button" onClick={onEditKey} className="flex-1 rounded-lg border border-gray-200 px-3 py-2 text-[10px] font-medium text-gray-600 transition hover:bg-gray-50">{inherited ? "使用独立 Key" : "更新 Key"}</button>
            {provider.credential_source === "web_search" ? <button type="button" onClick={onDeleteKey} title="删除独立 Key" className="rounded-lg border border-gray-200 px-2.5 text-gray-400 transition hover:border-rose-200 hover:bg-rose-50 hover:text-rose-600"><Trash2 className="h-3.5 w-3.5" /></button> : null}
          </div>
        )}

        <div className="mt-2 flex gap-2">
          <button type="button" disabled={busy || !provider.credential_configured || provider.credential_readable === false} onClick={onTest} className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-gray-200 px-3 py-2 text-[10px] font-medium text-gray-600 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-45">{busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}测试</button>
          <button type="button" disabled={busy || (!provider.enabled && provider.state !== "ready")} onClick={onToggle} title={!provider.enabled && provider.state !== "ready" ? "请先通过连接测试" : undefined} className={`flex-1 rounded-lg px-3 py-2 text-[10px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-45 ${provider.enabled ? "border border-gray-200 text-gray-600 hover:bg-gray-50" : "bg-[#002fa7] text-white hover:bg-[#001f7a]"}`}>{busy ? "处理中…" : provider.enabled ? "停用" : "启用"}</button>
          <a href={provider.docs} target="_blank" rel="noreferrer" title="查看官方文档" className="flex items-center justify-center rounded-lg border border-gray-200 px-2.5 text-gray-400 transition hover:bg-gray-50 hover:text-gray-700"><ExternalLink className="h-3.5 w-3.5" /></a>
        </div>
        {provider.last_error ? <p className="mt-2 line-clamp-2 text-[9px] leading-4 text-rose-600">{provider.last_error}</p> : null}
      </div>
    </article>
  );
}

function CapabilityTag({ label, active }: { label: string; active: boolean }) {
  return <span className={`rounded-md px-2 py-1 text-[9px] font-medium ${active ? "bg-[#002fa7]/[0.07] text-[#002fa7]" : "bg-gray-100 text-gray-400 line-through"}`}>{label}</span>;
}

function MiniOption({ label, checked, disabled, onChange }: { label: string; checked: boolean; disabled: boolean; onChange: () => void }) {
  return <div className="flex items-center justify-between rounded-lg border border-black/[0.055] bg-gray-50 px-2.5 py-2"><span className="text-[9px] font-medium text-gray-600">{label}</span><Switch checked={checked} onChange={onChange} disabled={disabled} label={label} /></div>;
}

function OptionRow({ title, description, checked, onChange, disabled }: { title: string; description: string; checked: boolean; onChange: () => void; disabled: boolean }) {
  return <div className="flex items-center justify-between gap-3 rounded-xl bg-gray-50 px-3.5 py-3"><div><p className="text-[11px] font-medium text-gray-700">{title}</p><p className="mt-0.5 text-[10px] text-gray-400">{description}</p></div><Switch checked={checked} onChange={onChange} disabled={disabled} label={title} /></div>;
}

function SelectOptionRow({ title, description, value, onChange, disabled }: { title: string; description: string; value: number; onChange: (value: number) => void; disabled: boolean }) {
  return (
    <label className="flex items-center justify-between gap-3 rounded-xl bg-gray-50 px-3.5 py-3">
      <span><span className="block text-[11px] font-medium text-gray-700">{title}</span><span className="mt-0.5 block text-[10px] text-gray-400">{description}</span></span>
      <select value={value} disabled={disabled} onChange={(event) => onChange(Number(event.target.value))} className="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[10px] font-semibold text-gray-700 outline-none focus:border-[#002fa7] disabled:opacity-45">
        <option value={1}>1 家</option><option value={2}>2 家</option><option value={3}>3 家</option>
      </select>
    </label>
  );
}

function RouteColumn({ title, description, scope, order, providers, busy, onMove }: { title: string; description: string; scope: "domestic" | "global"; order: WebSearchProviderId[]; providers: Map<WebSearchProviderId, WebSearchProvider>; busy: boolean; onMove: (scope: "domestic" | "global", providerId: WebSearchProviderId, direction: -1 | 1) => void }) {
  return (
    <div className="rounded-xl border border-black/[0.055] bg-gray-50/70 p-3.5">
      <div className="mb-3"><h3 className="text-[12px] font-semibold text-gray-800">{title}</h3><p className="mt-0.5 text-[10px] text-gray-400">{description}</p></div>
      <div className="space-y-2">
        {order.map((providerId, index) => {
          const provider = providers.get(providerId);
          if (!provider) return null;
          return (
            <div key={providerId} className="flex items-center gap-2.5 rounded-lg border border-black/[0.05] bg-white px-2.5 py-2 shadow-sm">
              <span className="w-4 text-center text-[10px] font-semibold text-gray-300">{index + 1}</span>
              <ProviderMark providerId={providerId} size="sm" />
              <div className="min-w-0 flex-1"><p className="truncate text-[10px] font-semibold text-gray-700">{provider.name}</p><p className={`mt-0.5 text-[9px] ${provider.enabled ? "text-emerald-600" : "text-gray-400"}`}>{provider.enabled ? "已启用" : "未启用，运行时跳过"}</p></div>
              <div className="flex gap-1">
                <button type="button" disabled={busy || index === 0} onClick={() => onMove(scope, providerId, -1)} className="rounded-md p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700 disabled:opacity-25"><ArrowUp className="h-3.5 w-3.5" /></button>
                <button type="button" disabled={busy || index === order.length - 1} onClick={() => onMove(scope, providerId, 1)} className="rounded-md p-1 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700 disabled:opacity-25"><ArrowDown className="h-3.5 w-3.5" /></button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
