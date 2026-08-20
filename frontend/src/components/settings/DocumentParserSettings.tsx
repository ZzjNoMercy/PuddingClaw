"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Cloud, Download, HardDrive, KeyRound, Loader2, RefreshCw, Save, Zap } from "lucide-react";

import {
  listDocumentParsers,
  installDocumentParserDependency,
  testDocumentParser,
  updateDocumentParser,
  type DocumentParserStatus,
} from "@/lib/api";

type ParserCategory = "local" | "mineru" | "llama";

const PARSER_CATEGORIES: Array<{ id: ParserCategory; label: string }> = [
  { id: "local", label: "本地解析" },
  { id: "mineru", label: "MinerU" },
  { id: "llama", label: "LlamaParse" },
];
const SUCCESS_MESSAGE_TTL_MS = 3000;

function parserCategory(parserId: string): ParserCategory {
  if (parserId.startsWith("mineru_")) return "mineru";
  if (parserId === "llama_parse_cloud") return "llama";
  return "local";
}

function Toggle({ checked, disabled, onChange }: { checked: boolean; disabled?: boolean; onChange: (next: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 shrink-0 rounded-full transition ${checked ? "bg-[#002fa7]" : "bg-gray-300"} disabled:cursor-not-allowed disabled:opacity-50`}
    >
      <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow-sm transition ${checked ? "left-[22px]" : "left-0.5"}`} />
    </button>
  );
}

export default function DocumentParserSettings() {
  const [parsers, setParsers] = useState<DocumentParserStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [activeCategory, setActiveCategory] = useState<ParserCategory>("local");
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [pageError, setPageError] = useState("");
  const [cardMessages, setCardMessages] = useState<Record<string, { ok: boolean; text: string }>>({});
  const messageTimers = useRef<Record<string, number>>({});

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await listDocumentParsers();
      setParsers(next);
      setUrls(Object.fromEntries(next.map((item) => [item.id, item.base_url || ""])));
      setPageError("");
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "加载解析器失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => () => {
    Object.values(messageTimers.current).forEach((timer) => window.clearTimeout(timer));
  }, []);
  useEffect(() => {
    if (!parsers.some((item) => item.dependency_install?.status === "installing")) return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [parsers, refresh]);
  const configured = useMemo(() => parsers.filter((item) => item.enabled).length, [parsers]);
  const categoryStats = useMemo(() => Object.fromEntries(PARSER_CATEGORIES.map((category) => {
    const items = parsers.filter((parser) => parserCategory(parser.id) === category.id);
    return [category.id, { total: items.length, available: items.filter((parser) => parser.available).length }];
  })) as Record<ParserCategory, { total: number; available: number }>, [parsers]);
  const visibleParsers = useMemo(
    () => parsers.filter((parser) => parserCategory(parser.id) === activeCategory),
    [activeCategory, parsers],
  );

  const setCardMessage = useCallback((parserId: string, message: { ok: boolean; text: string }) => {
    if (messageTimers.current[parserId]) {
      window.clearTimeout(messageTimers.current[parserId]);
      delete messageTimers.current[parserId];
    }
    setCardMessages((current) => ({ ...current, [parserId]: message }));
    if (message.ok) {
      messageTimers.current[parserId] = window.setTimeout(() => {
        setCardMessages((current) => {
          if (current[parserId] !== message) return current;
          const next = { ...current };
          delete next[parserId];
          return next;
        });
        delete messageTimers.current[parserId];
      }, SUCCESS_MESSAGE_TTL_MS);
    }
  }, []);

  const save = useCallback(async (parser: DocumentParserStatus, patch: Parameters<typeof updateDocumentParser>[1]) => {
    setBusy(`${parser.id}:save`);
    try {
      await updateDocumentParser(parser.id, patch);
      setKeys((current) => ({ ...current, [parser.id]: "" }));
      await refresh();
      setCardMessage(parser.id, { ok: true, text: "配置已保存" });
    } catch (error) {
      setCardMessage(parser.id, { ok: false, text: error instanceof Error ? error.message : "保存失败" });
    } finally {
      setBusy("");
    }
  }, [refresh, setCardMessage]);

  const test = useCallback(async (parser: DocumentParserStatus) => {
    setBusy(`${parser.id}:test`);
    try {
      const hasDraftConnection = Boolean(keys[parser.id]?.trim())
        || (urls[parser.id] || "").trim() !== (parser.base_url || "").trim();
      const result = await testDocumentParser(parser.id, {
        base_url: urls[parser.id],
        ...(keys[parser.id]?.trim() ? { api_key: keys[parser.id].trim() } : {}),
      });
      setCardMessage(parser.id, {
        ok: result.ok,
        text: result.ok && hasDraftConnection
          ? `测试通过，保存后生效：${result.message}`
          : `测试结果：${result.message}`,
      });
    } catch (error) {
      setCardMessage(parser.id, { ok: false, text: error instanceof Error ? error.message : "测试失败" });
    } finally {
      setBusy("");
    }
  }, [keys, setCardMessage, urls]);

  const install = useCallback(async (parser: DocumentParserStatus) => {
    setBusy(`${parser.id}:install`);
    try {
      const state = await installDocumentParserDependency(parser.id);
      setCardMessage(parser.id, { ok: true, text: state.message || `${parser.name} 依赖安装已开始` });
      await refresh();
    } catch (error) {
      setCardMessage(parser.id, { ok: false, text: error instanceof Error ? error.message : "安装失败" });
    } finally {
      setBusy("");
    }
  }, [refresh, setCardMessage]);

  if (loading && !parsers.length) {
    return <div className="flex min-h-40 items-center justify-center text-sm text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />加载解析器状态</div>;
  }

  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-blue-100 bg-blue-50/50 px-3 py-2 text-[11px] leading-5 text-blue-700">
        解析器只把原始文件转换成可迁移 Markdown、图片和结构元数据。LlamaIndex 统一负责后续切片；Embedding、LLM Wiki 与 GBrain 都不会被某个解析器私自执行。
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <nav aria-label="解析器分类" className="flex items-center gap-1 rounded-xl border border-black/[0.07] bg-black/[0.025] p-1">
          {PARSER_CATEGORIES.map((category) => {
            const stats = categoryStats[category.id];
            const active = activeCategory === category.id;
            return (
              <button
                key={category.id}
                type="button"
                aria-pressed={active}
                onClick={() => setActiveCategory(category.id)}
                className={`flex h-9 items-center gap-2 rounded-lg px-3 text-[11px] font-medium transition ${active ? "bg-white text-[#002fa7] shadow-sm ring-1 ring-black/[0.04]" : "text-gray-500 hover:bg-white/60 hover:text-gray-800"}`}
              >
                <span>{category.label}</span>
                <span className={`rounded-full px-1.5 py-0.5 font-mono text-[9px] ${active ? "bg-blue-50 text-[#002fa7]" : "bg-black/[0.045] text-gray-400"}`}>
                  {stats.available}/{stats.total}
                </span>
              </button>
            );
          })}
        </nav>
        <div className="flex items-center gap-3">
          <span className="text-[10px] text-gray-400">全局启用 {configured}/{parsers.length}</span>
          <button type="button" onClick={() => void refresh()} disabled={loading} className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-black/[0.08] bg-white px-3 text-[11px] font-medium text-gray-600 hover:text-[#002fa7] disabled:opacity-50">
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />刷新
          </button>
        </div>
      </div>

      {pageError ? (
        <div className="rounded-xl border border-red-100 bg-red-50 px-3.5 py-3 text-[11px] text-red-700">
          {pageError}
        </div>
      ) : null}

      <div className="space-y-2">
        {visibleParsers.map((parser) => {
          const showBaseUrl = parser.id === "mineru_local" || parser.location === "cloud";
          const hasConfiguration = showBaseUrl || parser.requires_credential;
          const installing = parser.dependency_install?.status === "installing";
          const dependencyNeedsInstall = Boolean(parser.dependency_extra && parser.healthy === false);
          const showControlRow = hasConfiguration || dependencyNeedsInstall;
          const hasDraftKey = Boolean(keys[parser.id]?.trim());
          const hasDraftBaseUrl = showBaseUrl
            && (urls[parser.id] || "").trim() !== (parser.base_url || "").trim();
          const hasDraftConnection = hasDraftKey || hasDraftBaseUrl;
          const credentialState = hasDraftKey
            ? parser.credential_configured ? "已修改" : "待保存"
            : parser.credential_configured ? "已保存" : "未配置";
          return (
            <div key={parser.id} className="rounded-xl border border-black/[0.07] bg-white/70 p-3">
              <div className="grid grid-cols-[2rem_minmax(0,1fr)_auto] items-start gap-2.5">
                <span className={`flex h-8 w-8 items-center justify-center rounded-lg ${parser.location === "cloud" ? "bg-violet-50 text-violet-600" : "bg-emerald-50 text-emerald-700"}`}>
                  {parser.location === "cloud" ? <Cloud className="h-4 w-4" /> : <HardDrive className="h-4 w-4" />}
                </span>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <p className="text-[12px] font-semibold text-gray-900">{parser.name}</p>
                    <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[9px] text-gray-500">{parser.location === "cloud" ? "云端" : "本地"}</span>
                    <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-medium ${hasDraftConnection ? "bg-blue-50 text-blue-700" : !parser.implementation_available ? "bg-amber-50 text-amber-700" : parser.enabled ? "bg-emerald-50 text-emerald-700" : "bg-gray-100 text-gray-500"}`}>
                      {hasDraftConnection ? "待保存" : !parser.implementation_available ? "尚未开放" : parser.enabled ? "已启用" : "已停用"}
                    </span>
                  </div>
                  <p className="mt-0.5 line-clamp-1 text-[10px] leading-4 text-gray-500">{parser.description}</p>
                  <p className="mt-0.5 truncate font-mono text-[9px] text-gray-400">{parser.id} · {parser.supported_extensions.join(" / ")}</p>
                </div>
                <Toggle
                  checked={parser.enabled}
                  disabled={busy.startsWith(parser.id) || !parser.implementation_available}
                  onChange={(enabled) => void save(parser, { enabled })}
                />
              </div>

              {parser.enabled && !parser.available && !hasDraftConnection && (parser.health_message || parser.dependency_install?.message) ? (
                <div className="mt-2 flex items-center gap-1.5 text-[10px] text-gray-500">
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400"></span>
                  <span className="truncate">{parser.health_message}{parser.dependency_install?.message ? ` · ${parser.dependency_install.message}` : ""}</span>
                </div>
              ) : null}

              {parser.implementation_available && showControlRow ? (
                <div className={`mt-2 border-t border-black/[0.05] pt-2 ${hasConfiguration ? "grid gap-2 md:grid-cols-[minmax(12rem,1fr)_minmax(12rem,1fr)_auto] md:items-end" : "flex items-center justify-end gap-2"}`}>
                  {showBaseUrl ? (
                    <label className="block min-w-0">
                      <span className="mb-1 block text-[9px] font-medium text-gray-400">API 地址</span>
                      <input value={urls[parser.id] || ""} onChange={(event) => {
                        setUrls((current) => ({ ...current, [parser.id]: event.target.value }));
                        setCardMessages((current) => {
                          const next = { ...current };
                          delete next[parser.id];
                          return next;
                        });
                      }} className="form-input h-8 text-[11px]" placeholder="API Base URL" />
                    </label>
                  ) : hasConfiguration ? <span></span> : null}
                  {parser.requires_credential ? (
                    <label className="block min-w-0">
                      <span className="mb-1 flex items-center gap-1 text-[9px] font-medium text-gray-400"><KeyRound className="h-2.5 w-2.5" />API Key · {credentialState}</span>
                      <input type="password" value={keys[parser.id] || ""} onChange={(event) => {
                        setKeys((current) => ({ ...current, [parser.id]: event.target.value }));
                        setCardMessages((current) => {
                          const next = { ...current };
                          delete next[parser.id];
                          return next;
                        });
                      }} className="form-input h-8 text-[11px]" placeholder={parser.credential_configured ? "留空保留现有密钥" : parser.credential_env || "输入 API Key"} autoComplete="new-password" />
                    </label>
                  ) : hasConfiguration ? <span></span> : null}
                  <div className="flex items-center justify-end gap-1.5">
                    {dependencyNeedsInstall ? (
                      <button type="button" onClick={() => void install(parser)} disabled={busy.startsWith(parser.id) || installing} className="inline-flex h-8 items-center gap-1 rounded-lg border border-black/[0.08] px-2.5 text-[10px] font-medium text-gray-600 hover:text-[#002fa7] disabled:opacity-50">
                        {installing || busy === `${parser.id}:install` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}安装依赖
                      </button>
                    ) : null}
                    {hasConfiguration ? (
                      <button type="button" onClick={() => void test(parser)} disabled={busy.startsWith(parser.id)} className="inline-flex h-8 items-center gap-1 rounded-lg border border-black/[0.08] px-2.5 text-[10px] font-medium text-gray-600 hover:text-[#002fa7] disabled:opacity-50">
                        {busy === `${parser.id}:test` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}测试
                      </button>
                    ) : null}
                    {hasConfiguration ? (
                      <button type="button" onClick={() => void save(parser, { base_url: urls[parser.id], api_key: keys[parser.id] })} disabled={busy.startsWith(parser.id)} className="inline-flex h-8 items-center gap-1 rounded-lg bg-[#002fa7] px-2.5 text-[10px] font-medium text-white hover:bg-[#001f7a] disabled:opacity-50">
                        {busy === `${parser.id}:save` ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}保存
                      </button>
                    ) : null}
                  </div>
                </div>
              ) : null}

              {cardMessages[parser.id] ? (
                <div role="status" aria-live="polite" className={`mt-2 rounded-lg border px-2.5 py-2 text-[10px] ${cardMessages[parser.id].ok ? "border-emerald-100 bg-emerald-50 text-emerald-700" : "border-red-100 bg-red-50 text-red-700"}`}>
                  {cardMessages[parser.id].text}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
