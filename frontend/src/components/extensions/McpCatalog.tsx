"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { createPortal } from "react-dom";
import {
  Check,
  CheckCircle2,
  ChevronRight,
  Database,
  Loader2,
  Plus,
  Save,
  Server,
  Settings2,
  X,
  XCircle,
} from "lucide-react";

import "@/lib/monaco-config";
import {
  getMcpConfig,
  getMcpServersStatus,
  updateMcpConfig,
  type McpConfig,
  type McpServerConfig,
  type McpServerStatus,
  type McpServersStatus,
} from "@/lib/api";

const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => <div className="flex h-full items-center justify-center text-xs text-gray-400">加载编辑器…</div>,
});

const STATUS_META: Record<McpServerStatus["status"], { label: string; dot: string; badge: string; healthy: boolean }> = {
  loaded: { label: "已加载", dot: "bg-emerald-500", badge: "bg-emerald-50 text-emerald-700", healthy: true },
  ready: { label: "可加载", dot: "bg-blue-500", badge: "bg-blue-50 text-blue-700", healthy: true },
  not_ready: { label: "未就绪", dot: "bg-gray-400", badge: "bg-gray-100 text-gray-600", healthy: false },
  error: { label: "加载失败", dot: "bg-red-500", badge: "bg-red-50 text-red-700", healthy: false },
};
const DISABLED_STATUS = { label: "未启用", dot: "bg-gray-400", badge: "bg-gray-100 text-gray-600", healthy: false };

function normalizeConfig(value: Partial<McpConfig> | null | undefined): McpConfig {
  return {
    enabled: Array.isArray(value?.enabled) ? value.enabled.filter((item): item is string => typeof item === "string" && item !== "gbrain") : [],
    servers: value?.servers && typeof value.servers === "object"
      ? Object.fromEntries(Object.entries(value.servers).filter(([key]) => key !== "gbrain"))
      : {},
  };
}

export default function McpCatalog() {
  const [data, setData] = useState<McpServersStatus | null>(null);
  const [config, setConfig] = useState<McpConfig | null>(null);
  const [configPath, setConfigPath] = useState("");
  const [loading, setLoading] = useState(true);
  const [probing, setProbing] = useState(false);
  const [error, setError] = useState("");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [showConfig, setShowConfig] = useState(false);
  const [showNewServer, setShowNewServer] = useState(false);
  const [saving, setSaving] = useState(false);
  const refreshRevision = useRef(0);

  const refresh = useCallback(async () => {
    const revision = ++refreshRevision.current;
    setLoading(true);
    setError("");
    try {
      const [status, savedConfig] = await Promise.all([getMcpServersStatus(false), getMcpConfig()]);
      if (revision !== refreshRevision.current) return;
      setData(status);
      setConfig(normalizeConfig(savedConfig.config));
      setConfigPath(savedConfig.path);
      setProbing(true);
      void getMcpServersStatus(true)
        .then((probed) => {
          if (revision === refreshRevision.current) setData(probed);
        })
        .catch((value) => {
          if (revision === refreshRevision.current) {
            setError(value instanceof Error ? value.message : "MCP 状态刷新失败");
          }
        })
        .finally(() => {
          if (revision === refreshRevision.current) setProbing(false);
        });
    } catch (value) {
      if (revision !== refreshRevision.current) return;
      setError(value instanceof Error ? value.message : "MCP 配置读取失败");
    } finally {
      if (revision === refreshRevision.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => { refreshRevision.current += 1; };
  }, [refresh]);

  const catalog = data?.catalog || [];
  const selected = catalog.find((server) => server.key === selectedKey) || null;
  const currentConfig = config || normalizeConfig(null);

  const saveConfig = useCallback(async (next: McpConfig) => {
    setSaving(true);
    try {
      const saved = await updateMcpConfig(next);
      setConfig(normalizeConfig(saved.config));
      setConfigPath(saved.path);
      setShowConfig(false);
      setShowNewServer(false);
      await refresh();
    } catch (value) {
      setError(value instanceof Error ? value.message : "MCP 配置保存失败");
      throw value;
    } finally {
      setSaving(false);
    }
  }, [refresh]);

  const toggleServer = useCallback(async (key: string) => {
    if (key === "gbrain") return;
    const enabled = currentConfig.enabled.includes(key);
    await saveConfig({ ...currentConfig, enabled: enabled ? currentConfig.enabled.filter((item) => item !== key) : [...currentConfig.enabled, key] });
  }, [currentConfig, saveConfig]);

  return (
    <div className="flex-1 overflow-y-auto bg-white/30">
      <div className="w-full px-5 pb-8 pt-3">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-black/[0.06] bg-white px-4 py-3 shadow-sm">
          <div>
            <p className="text-sm font-semibold text-gray-800">MCP 服务管理</p>
            <p className="mt-1 flex items-center gap-1.5 text-xs text-gray-500">
              所有 MCP Server 都统一配置在同一个文件中。
              {probing ? <span className="inline-flex items-center gap-1 text-gray-400"><Loader2 className="h-3 w-3 animate-spin" />正在刷新状态</span> : null}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => setShowNewServer(true)} className="inline-flex items-center gap-1.5 rounded-xl border border-[#002fa7]/20 px-3 py-2 text-xs font-semibold text-[#002fa7] hover:bg-[#002fa7]/[0.05]"><Plus className="h-3.5 w-3.5" /> 添加 MCP</button>
            <button type="button" onClick={() => setShowConfig(true)} className="inline-flex items-center gap-1.5 rounded-xl bg-[#002fa7] px-3 py-2 text-xs font-semibold text-white shadow-sm hover:bg-[#001f7a]"><Settings2 className="h-3.5 w-3.5" /> 编辑配置文件</button>
          </div>
        </div>

        {configPath ? <p className="mb-3 truncate font-mono text-[10px] text-gray-400" title={configPath}>当前配置：{configPath}</p> : null}
        {error ? (
          <div className="mb-3 rounded-xl border border-red-100 bg-red-50 px-4 py-3 text-xs text-red-700">
            {error}<button type="button" onClick={() => void refresh()} className="ml-3 font-medium underline">重试</button>
          </div>
        ) : null}

        {loading && !data ? (
          <div className="flex justify-center py-20"><Loader2 className="h-5 w-5 animate-spin text-gray-400" /></div>
        ) : catalog.length ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {catalog.map((server) => {
              const status = server.key === "gbrain" && !server.ready
                ? STATUS_META.not_ready
                : server.enabled ? STATUS_META[server.status] : DISABLED_STATUS;
              const Icon = server.key === "gbrain" ? Database : Server;
              return (
                <button key={server.key} type="button" onClick={() => setSelectedKey(server.key)} className="group flex min-h-[104px] items-start gap-3 rounded-xl border border-black/[0.06] bg-white px-3 py-3 text-left shadow-sm transition-all hover:border-[#002fa7]/20 hover:shadow-md">
                  <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-gray-400 transition-colors group-hover:bg-[#002fa7]/10 group-hover:text-[#002fa7]"><Icon className="h-5 w-5" /></span>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="truncate text-[14px] font-semibold text-gray-800">{server.name}</span>
                      <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-medium ${status.badge}`}><span className={`h-1.5 w-1.5 rounded-full ${status.dot}`} />{status.label}</span>
                    </span>
                    <span className="mt-1 block truncate font-mono text-[11px] text-gray-400">{server.key} · {server.transport}</span>
                    <span className="mt-1.5 flex items-center gap-2 text-[11px] text-gray-500"><span>{server.enabled ? `${server.tool_count} 个工具` : server.key === "gbrain" ? "等待初始化" : "尚未启用"}</span>{server.auto_enabled ? <span>· 内置服务</span> : null}</span>
                  </span>
                  <ChevronRight className="mt-2 h-4 w-4 shrink-0 text-gray-300 transition-transform group-hover:translate-x-0.5 group-hover:text-[#002fa7]" />
                </button>
              );
            })}
          </div>
        ) : !error ? <div className="rounded-2xl border border-dashed border-black/10 px-5 py-14 text-center text-sm text-gray-400">没有已配置的 MCP 服务</div> : null}
      </div>

      {selected ? <McpServerModal server={selected} gbrain={selected.key === "gbrain" ? data?.gbrain || null : null} onToggle={() => toggleServer(selected.key)} onClose={() => setSelectedKey(null)} /> : null}
      {showConfig ? <McpConfigModal path={configPath} config={currentConfig} saving={saving} onClose={() => setShowConfig(false)} onSave={saveConfig} /> : null}
      {showNewServer ? <NewMcpServerModal existingKeys={catalog.map((server) => server.key)} saving={saving} onClose={() => setShowNewServer(false)} onSave={(server) => saveConfig({ ...currentConfig, servers: { ...currentConfig.servers, [server.key]: server.config }, enabled: server.enabled ? [...currentConfig.enabled, server.key] : currentConfig.enabled })} /> : null}
    </div>
  );
}

function McpServerModal({ server, gbrain, onToggle, onClose }: { server: McpServerStatus; gbrain: McpServersStatus["gbrain"] | null; onToggle: () => Promise<void>; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const status = server.key === "gbrain" && !server.ready
    ? STATUS_META.not_ready
    : server.enabled ? STATUS_META[server.status] : DISABLED_STATUS;
  const Icon = server.key === "gbrain" ? Database : Server;
  useEffect(() => {
    closeRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);
  const handleToggle = async () => { setBusy(true); try { await onToggle(); onClose(); } finally { setBusy(false); } };
  return createPortal(
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-[2px]" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section role="dialog" aria-modal="true" aria-labelledby="mcp-server-title" className="relative max-h-[calc(100vh-2rem)] w-full max-w-[700px] overflow-y-auto rounded-3xl border border-white/60 bg-white p-6 shadow-2xl sm:p-8">
        <button ref={closeRef} type="button" onClick={onClose} aria-label="关闭 MCP 服务详情" className="absolute right-5 top-5 flex h-9 w-9 items-center justify-center rounded-full text-gray-400 hover:bg-gray-100 hover:text-gray-700"><X className="h-5 w-5" /></button>
        <div className="flex items-start gap-4 pr-10"><span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.07] text-[#002fa7]"><Icon className="h-6 w-6" /></span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h2 id="mcp-server-title" className="text-xl font-semibold text-gray-950">{server.name}</h2><span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ${status.badge}`}>{status.healthy ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}{status.label}</span></div><p className="mt-1 font-mono text-xs text-gray-400">{server.key} · {server.transport}</p></div></div>
        {server.reason ? <div className="mt-5 rounded-xl border border-red-100 bg-red-50 px-4 py-3 text-xs leading-5 text-red-700">{server.reason}</div> : null}
        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-3"><SummaryItem label="工具" value={server.enabled ? `${server.tool_count} 个` : "未探测"} /><SummaryItem label="传输方式" value={server.transport} /><SummaryItem label="服务类型" value={server.key === "gbrain" ? "内置服务" : "用户服务"} /></div>
        {gbrain?.home ? <div className="mt-5 rounded-xl bg-slate-50 px-4 py-3"><p className="text-[11px] font-medium text-gray-500">运行目录</p><code className="mt-1 block break-all font-mono text-[11px] leading-5 text-gray-700">{gbrain.home}</code></div> : null}
        {gbrain?.models ? <div className="mt-5"><h3 className="text-[12px] font-semibold text-gray-700">运行模型</h3><div className="mt-2 grid gap-2 sm:grid-cols-2"><SummaryItem label="Embedding" value={gbrain.models.embedding ? `${gbrain.models.embedding.provider}:${gbrain.models.embedding.name}${gbrain.models.embedding.dimension ? ` · ${gbrain.models.embedding.dimension} 维` : ""}` : "未配置"} /><SummaryItem label="Think" value={gbrain.models.think ? `${gbrain.models.think.provider}:${gbrain.models.think.name}` : "未配置"} /></div></div> : null}
        <div className="mt-6 border-t border-black/[0.06] pt-5">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-[12px] font-semibold text-gray-700">工具列表</h3>
            <span className="text-[11px] text-gray-400">{server.tools.length} 个工具</span>
          </div>
          {server.tools.length ? <div className="mt-3 flex max-h-48 flex-wrap content-start gap-1.5 overflow-y-auto pr-1">{server.tools.map((tool) => <code key={tool} className="rounded-lg bg-slate-100 px-2 py-1 text-[10px] text-slate-600">{tool}</code>)}</div> : <p className="mt-2 text-xs text-gray-400">{server.key === "gbrain" ? gbrain?.ready ? "内置服务已就绪，工具正在加载。" : "内置服务尚未完成初始化。" : "当前没有可用工具，请先启用并加载 MCP。"}</p>}
        </div>
        <div className="mt-6 flex justify-end gap-2 border-t border-black/[0.06] pt-5">{server.key === "gbrain" ? <button type="button" onClick={onClose} className="rounded-xl bg-[#002fa7] px-5 py-2 text-sm font-semibold text-white hover:bg-[#001f7a]">关闭</button> : <><button type="button" onClick={onClose} className="rounded-xl border border-black/10 px-4 py-2 text-sm text-gray-600 hover:bg-gray-50">取消</button><button type="button" onClick={() => void handleToggle()} disabled={busy} className="inline-flex items-center gap-2 rounded-xl bg-[#002fa7] px-4 py-2 text-sm font-semibold text-white hover:bg-[#001f7a] disabled:opacity-50">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : server.enabled ? <XCircle className="h-4 w-4" /> : <Check className="h-4 w-4" />}{server.enabled ? "停用 MCP" : "启用 MCP"}</button></>}</div>
      </section>
    </div>, document.body,
  );
}

function McpConfigModal({ path, config, saving, onClose, onSave }: { path: string; config: McpConfig; saving: boolean; onClose: () => void; onSave: (config: McpConfig) => Promise<void> }) {
  const [content, setContent] = useState(() => JSON.stringify(config, null, 2));
  const [parseError, setParseError] = useState("");
  const dirty = content !== JSON.stringify(config, null, 2);
  const readDraft = () => {
    try {
      const parsed = JSON.parse(content) as McpConfig;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("配置必须是 JSON 对象");
      return normalizeConfig(parsed);
    } catch (value) {
      setParseError(value instanceof Error ? value.message : "JSON 格式不正确");
      return null;
    }
  };
  const save = async () => {
    const draft = readDraft();
    if (!draft) return;
    setParseError("");
    await onSave(draft);
  };
  return createPortal(<div className="fixed inset-0 z-[120] flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-[2px]"><section role="dialog" aria-modal="true" aria-labelledby="mcp-config-title" className="flex h-[min(800px,calc(100vh-2rem))] w-full max-w-[960px] flex-col overflow-hidden rounded-3xl bg-white shadow-2xl"><div className="flex shrink-0 items-start justify-between border-b border-black/[0.06] px-6 py-5"><div><h2 id="mcp-config-title" className="text-xl font-semibold text-gray-950">MCP 配置文件</h2><p className="mt-1 text-sm text-gray-500">所有 MCP Server、启用状态和连接参数都在这一个文件中配置。</p></div><button type="button" onClick={onClose} className="flex h-9 w-9 items-center justify-center rounded-full text-gray-400 hover:bg-gray-100"><X className="h-5 w-5" /></button></div><div className="shrink-0 bg-gray-50 px-6 py-3 text-xs text-gray-600">配置文件路径：<code className="font-mono text-gray-800">{path || "~/.puddingclaw/config.json"}</code></div><div className="min-h-0 flex-1"><MonacoEditor height="100%" language="json" theme="vs" value={content} onChange={(value) => { setContent(value || ""); setParseError(""); }} options={{ minimap: { enabled: false }, fontSize: 13, lineNumbers: "on", wordWrap: "on", scrollBeyondLastLine: false, padding: { top: 12, bottom: 12 }, automaticLayout: true, formatOnPaste: true, formatOnType: true }} /></div>{parseError ? <p className="shrink-0 border-t border-red-100 bg-red-50 px-6 py-2 text-xs text-red-700">{parseError}</p> : null}<div className="flex shrink-0 justify-end gap-2 border-t border-black/[0.06] px-6 py-4"><button type="button" onClick={onClose} className="rounded-xl border border-black/10 px-5 py-2.5 text-sm text-gray-600 hover:bg-gray-50">取消</button><button type="button" onClick={() => void save()} disabled={saving || !dirty} className="inline-flex items-center gap-2 rounded-xl bg-[#002fa7] px-5 py-2.5 text-sm font-semibold text-white hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:bg-gray-300">{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}保存</button></div></section></div>, document.body);
}

function NewMcpServerModal({ existingKeys, saving, onClose, onSave }: { existingKeys: string[]; saving: boolean; onClose: () => void; onSave: (server: { key: string; config: McpServerConfig; enabled: boolean }) => Promise<void> }) {
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<McpServerConfig["transport"]>("streamable-http");
  const [url, setUrl] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [headers, setHeaders] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [formError, setFormError] = useState("");
  const controlClass = "w-full rounded-xl border border-gray-200 bg-white px-3.5 py-2.5 text-sm text-gray-700 outline-none transition-colors placeholder:text-gray-400 focus:border-[#002fa7]/50 focus:ring-2 focus:ring-[#002fa7]/10";

  const submit = async () => {
    const normalizedKey = key.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(normalizedKey)) {
      setFormError("标识只能包含字母、数字、点、下划线和连字符");
      return;
    }
    if (existingKeys.includes(normalizedKey)) {
      setFormError("该 MCP 标识已存在，请直接编辑配置文件");
      return;
    }
    if (transport !== "stdio" && !url.trim()) {
      setFormError("HTTP/SSE MCP 需要填写 URL");
      return;
    }
    if (transport === "stdio" && !command.trim()) {
      setFormError("stdio MCP 需要填写启动命令");
      return;
    }

    let parsedHeaders: Record<string, string> | undefined;
    if (headers.trim()) {
      try {
        const parsed = JSON.parse(headers);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
        parsedHeaders = parsed;
      } catch {
        setFormError("Headers 必须是 JSON 对象");
        return;
      }
    }

    setFormError("");
    await onSave({
      key: normalizedKey,
      enabled,
      config: {
        ...(name.trim() ? { name: name.trim() } : {}),
        transport,
        ...(transport === "stdio"
          ? { command: command.trim(), ...(args.trim() ? { args: args.split("\n").map((item) => item.trim()).filter(Boolean) } : {}) }
          : { url: url.trim() }),
        ...(parsedHeaders ? { headers: parsedHeaders } : {}),
      },
    });
  };

  return createPortal(
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-[2px]">
      <section role="dialog" aria-modal="true" aria-labelledby="new-mcp-title" className="max-h-[calc(100vh-2rem)] w-full max-w-[680px] overflow-y-auto rounded-3xl bg-white p-7 shadow-2xl sm:p-8">
        <div className="flex items-start justify-between gap-6">
          <div>
            <h2 id="new-mcp-title" className="text-xl font-semibold text-gray-950">添加 MCP 服务</h2>
            <p className="mt-2 text-sm text-gray-500">添加结果会写入当前 MCP 配置文件的 <code className="font-mono text-xs">mcp.servers</code>。</p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭添加 MCP" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-gray-400 hover:bg-gray-100"><X className="h-5 w-5" /></button>
        </div>

        <div className="mt-8 grid gap-x-6 gap-y-6 sm:grid-cols-2">
          <Field label="标识 *"><input className={controlClass} value={key} onChange={(event) => setKey(event.target.value)} placeholder="my-mcp" /></Field>
          <Field label="显示名称"><input className={controlClass} value={name} onChange={(event) => setName(event.target.value)} placeholder="我的 MCP 服务" /></Field>
          <Field label="传输方式"><select className={controlClass} value={transport} onChange={(event) => setTransport(event.target.value as McpServerConfig["transport"])}><option value="streamable-http">streamable-http</option><option value="sse">sse</option><option value="stdio">stdio</option></select></Field>
          {transport === "stdio" ? <Field label="启动命令 *"><input className={controlClass} value={command} onChange={(event) => setCommand(event.target.value)} placeholder="npx" /></Field> : <Field label="服务 URL *"><input className={controlClass} value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/mcp" /></Field>}
        </div>

        <div className="mt-6 space-y-6">
          {transport === "stdio" ? <Field label="启动参数（每行一个）"><textarea className={controlClass + " min-h-[96px] resize-y"} value={args} onChange={(event) => setArgs(event.target.value)} rows={3} placeholder="-y\n@modelcontextprotocol/server-filesystem" /></Field> : null}
          <Field label="Headers（JSON，可选）"><textarea className={controlClass + " min-h-[96px] resize-y font-mono text-xs"} value={headers} onChange={(event) => setHeaders(event.target.value)} rows={3} placeholder={'{"Authorization":"\${ENV_NAME}"}'} /></Field>
        </div>

        <label className="mt-7 flex min-h-11 items-center gap-3 rounded-xl bg-gray-50 px-3.5 text-sm text-gray-700">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} className="h-4 w-4 rounded border-gray-300 text-[#002fa7]" />
          保存后立即启用
        </label>
        {formError ? <p className="mt-4 rounded-xl border border-red-100 bg-red-50 px-3.5 py-3 text-xs text-red-700">{formError}</p> : null}

        <div className="mt-8 flex justify-end gap-3 border-t border-black/[0.06] pt-6">
          <button type="button" onClick={onClose} className="rounded-xl border border-black/10 px-5 py-2.5 text-sm text-gray-600 hover:bg-gray-50">取消</button>
          <button type="button" onClick={() => void submit()} disabled={saving} className="inline-flex items-center gap-2 rounded-xl bg-[#002fa7] px-5 py-2.5 text-sm font-semibold text-white hover:bg-[#001f7a] disabled:opacity-50">{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}添加 MCP</button>
        </div>
      </section>
    </div>,
    document.body,
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block text-[13px] font-semibold text-gray-700">{label}<span className="mt-2 block">{children}</span></label>;
}

function SummaryItem({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0 rounded-xl bg-gray-50 px-3 py-2.5"><p className="text-[10px] text-gray-400">{label}</p><p className="mt-1 truncate text-[11px] font-medium text-gray-700" title={value}>{value}</p></div>;
}
