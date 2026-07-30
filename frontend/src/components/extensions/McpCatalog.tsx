"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  CheckCircle2,
  ChevronRight,
  Database,
  Loader2,
  Server,
  X,
  XCircle,
} from "lucide-react";

import {
  getMcpServersStatus,
  type McpServerStatus,
  type McpServersStatus,
} from "@/lib/api";

const STATUS_META: Record<
  McpServerStatus["status"],
  { label: string; dot: string; badge: string; healthy: boolean }
> = {
  loaded: {
    label: "已加载",
    dot: "bg-emerald-500",
    badge: "bg-emerald-50 text-emerald-700",
    healthy: true,
  },
  ready: {
    label: "可加载",
    dot: "bg-blue-500",
    badge: "bg-blue-50 text-blue-700",
    healthy: true,
  },
  not_ready: {
    label: "未就绪",
    dot: "bg-gray-400",
    badge: "bg-gray-100 text-gray-600",
    healthy: false,
  },
  error: {
    label: "加载失败",
    dot: "bg-red-500",
    badge: "bg-red-50 text-red-700",
    healthy: false,
  },
};

export default function McpCatalog() {
  const [data, setData] = useState<McpServersStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await getMcpServersStatus(true));
    } catch (value) {
      setError(value instanceof Error ? value.message : "MCP 状态读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const catalog = data?.catalog || [];
  const selected = catalog.find((server) => server.key === selectedKey) || null;

  return (
    <div className="flex-1 overflow-y-auto bg-white/30">
      <div className="w-full px-5 pb-8 pt-3">
        {error ? (
          <div className="mb-3 rounded-xl border border-red-100 bg-red-50 px-4 py-3 text-xs text-red-700">
            {error}
            <button type="button" onClick={() => void refresh()} className="ml-3 font-medium underline">
              重试
            </button>
          </div>
        ) : null}

        {loading && !data ? (
          <div className="flex justify-center py-20">
            <Loader2 className="h-5 w-5 animate-spin text-gray-400" />
          </div>
        ) : catalog.length ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {catalog.map((server) => {
              const status = STATUS_META[server.status];
              const Icon = server.key === "gbrain" ? Database : Server;
              return (
                <button
                  key={server.key}
                  type="button"
                  onClick={() => setSelectedKey(server.key)}
                  className="group flex min-h-[88px] items-start gap-3 rounded-xl border border-black/[0.06] bg-white px-3 py-3 text-left shadow-sm transition-all hover:border-[#002fa7]/20 hover:shadow-md"
                >
                  <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-gray-400 transition-colors group-hover:bg-[#002fa7]/10 group-hover:text-[#002fa7]">
                    <Icon className="h-5 w-5" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="truncate text-[14px] font-semibold text-gray-800">{server.name}</span>
                      <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-medium ${status.badge}`}>
                        <span className={`h-1.5 w-1.5 rounded-full ${status.dot}`} />
                        {status.label}
                      </span>
                    </span>
                    <span className="mt-1 block truncate font-mono text-[11px] text-gray-400">
                      {server.key} · {server.transport}
                    </span>
                    <span className="mt-1.5 flex items-center gap-2 text-[11px] text-gray-500">
                      <span>{server.tool_count} 个工具</span>
                      {server.auto_enabled ? <span>· 自动启用</span> : null}
                    </span>
                  </span>
                  <ChevronRight className="mt-2 h-4 w-4 shrink-0 text-gray-300 transition-transform group-hover:translate-x-0.5 group-hover:text-[#002fa7]" />
                </button>
              );
            })}
          </div>
        ) : !error ? (
          <div className="rounded-2xl border border-dashed border-black/10 px-5 py-14 text-center text-sm text-gray-400">
            没有已配置的 MCP 服务
          </div>
        ) : null}
      </div>

      {selected ? (
        <McpServerModal
          server={selected}
          models={selected.key === "gbrain" ? data?.gbrain.models || null : null}
          onClose={() => setSelectedKey(null)}
        />
      ) : null}
    </div>
  );
}

function McpServerModal({
  server,
  models,
  onClose,
}: {
  server: McpServerStatus;
  models: McpServersStatus["gbrain"]["models"] | null;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const status = STATUS_META[server.status];
  const Icon = server.key === "gbrain" ? Database : Server;

  useEffect(() => {
    const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
      returnFocus?.focus();
    };
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-[120] flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-[2px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="mcp-server-title"
        className="relative max-h-[calc(100vh-2rem)] w-full max-w-[700px] overflow-y-auto rounded-3xl border border-white/60 bg-white p-6 shadow-2xl sm:p-8"
      >
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          aria-label="关闭 MCP 服务详情"
          className="absolute right-5 top-5 flex h-9 w-9 items-center justify-center rounded-full text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700"
        >
          <X className="h-5 w-5" />
        </button>

        <div className="flex items-start gap-4 pr-10">
          <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.07] text-[#002fa7]">
            <Icon className="h-6 w-6" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 id="mcp-server-title" className="text-xl font-semibold text-gray-950">{server.name}</h2>
              <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ${status.badge}`}>
                {status.healthy ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                {status.label}
              </span>
            </div>
            <p className="mt-1 font-mono text-xs text-gray-400">{server.key} · {server.transport}</p>
          </div>
        </div>

        {server.reason ? (
          <div className="mt-5 rounded-xl border border-red-100 bg-red-50 px-4 py-3 text-xs leading-5 text-red-700">
            {server.reason}
          </div>
        ) : null}

        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-3">
          <SummaryItem label="工具" value={`${server.tool_count} 个`} />
          <SummaryItem label="传输方式" value={server.transport} />
          <SummaryItem label="加载策略" value={server.auto_enabled ? "自动启用" : "按需启用"} />
        </div>

        {models ? (
          <div className="mt-5">
            <h3 className="text-[12px] font-semibold text-gray-700">运行模型</h3>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              <SummaryItem
                label="Embedding"
                value={models.embedding ? `${models.embedding.provider}:${models.embedding.name}${models.embedding.dimension ? ` · ${models.embedding.dimension} 维` : ""}` : "未配置"}
              />
              <SummaryItem
                label="Think"
                value={models.think ? `${models.think.provider}:${models.think.name}` : "未配置"}
              />
            </div>
          </div>
        ) : null}

        <div className="mt-5 border-t border-black/[0.06] pt-5">
          <h3 className="text-[12px] font-semibold text-gray-700">安全筛选后的工具</h3>
          {server.tools.length ? (
            <div className="mt-3 flex max-h-52 flex-wrap content-start gap-1.5 overflow-y-auto pr-1">
              {server.tools.map((tool) => (
                <code key={tool} className="rounded-lg bg-slate-100 px-2 py-1 text-[10px] text-slate-600">
                  {tool}
                </code>
              ))}
            </div>
          ) : (
            <p className="mt-2 text-xs text-gray-400">当前没有可用工具</p>
          )}
        </div>
      </section>
    </div>,
    document.body,
  );
}

function SummaryItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-xl bg-gray-50 px-3 py-2.5">
      <p className="text-[10px] text-gray-400">{label}</p>
      <p className="mt-1 truncate text-[11px] font-medium text-gray-700" title={value}>{value}</p>
    </div>
  );
}
