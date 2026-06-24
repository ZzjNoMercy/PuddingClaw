"use client";

import { useEffect, useState, useCallback } from "react";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  Database,
  FileText,
  Loader2,
  Network,
  XCircle,
} from "lucide-react";
import { getCapabilities, type Capabilities, type CapabilityStatus } from "@/lib/settingsApi";

interface CapabilitiesStatusProps {
  refreshIntervalMs?: number;
}

const SERVICE_META: Record<
  keyof Capabilities,
  {
    label: string;
    description: string;
    icon: React.ElementType;
    color: string;
    details: { label: string; value: string }[];
  }
> = {
  ai_gateway: {
    label: "AI Gateway (Higress)",
    description: "统一模型路由、计量与限流",
    icon: Network,
    color: "#002fa7",
    details: [
      { label: "功能", value: "LLM 请求代理 / Token 计量 / 限流" },
      { label: "检测地址", value: "http://localhost:8080/health" },
      { label: "降级策略", value: "直接连接模型提供商" },
    ],
  },
  milvus: {
    label: "Milvus 向量库",
    description: "语义检索与多模态向量存储",
    icon: Database,
    color: "#7c3aed",
    details: [
      { label: "功能", value: "向量检索 / 记忆相似度匹配" },
      { label: "检测地址", value: "http://localhost:19530" },
      { label: "降级策略", value: "关闭 RAG，使用完整上下文" },
    ],
  },
  mineru: {
    label: "MinerU PDF 解析",
    description: "高质量 PDF/Office 版面解析",
    icon: FileText,
    color: "#10b981",
    details: [
      { label: "功能", value: "PDF 结构化提取 / 版面还原" },
      { label: "检测地址", value: "http://localhost:8002/health" },
      { label: "降级策略", value: "跳过文档解析，仅处理文本" },
    ],
  },
};

function StatusBadge({ available }: { available: boolean }) {
  return available ? (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50/80 px-2 py-0.5 text-[11px] font-medium text-emerald-600 border border-emerald-100/60">
      <span className="relative flex h-2 w-2">
        <span
          className="absolute inset-[-4px] rounded-full opacity-35 text-emerald-500"
          style={{ background: "currentColor", animation: "status-pulse-ring 2s cubic-bezier(0.215, 0.61, 0.355, 1) infinite" }}
        />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
      </span>
      可用
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500 border border-gray-200/60">
      <span className="rounded-full h-2 w-2 bg-gray-400" />
      未启用
    </span>
  );
}

function ServiceCard({
  serviceKey,
  service,
  expanded,
  onToggle,
}: {
  serviceKey: keyof Capabilities;
  service: CapabilityStatus;
  expanded: boolean;
  onToggle: () => void;
}) {
  const meta = SERVICE_META[serviceKey];
  const Icon = meta.icon;
  return (
    <div
      onClick={onToggle}
      className={`bg-white/60 backdrop-blur-xl rounded-2xl border border-black/[0.06] p-4 cursor-pointer transition-all duration-200 hover:bg-white/80 ${
        expanded ? "ring-1 ring-[#002fa7]/10 shadow-md" : "shadow-sm hover:shadow-md"
      }`}
    >
      <div className="flex items-start gap-3.5">
        <div
          className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
          style={{ background: `${meta.color}10`, color: meta.color }}
        >
          <Icon className="w-5 h-5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-[13px] font-semibold text-gray-800">{meta.label}</h3>
              <p className="text-[11px] text-gray-500 mt-0.5">{meta.description}</p>
            </div>
            <StatusBadge available={service.available} />
          </div>

          {!service.available && service.reason && (
            <div className="mt-3 flex items-start gap-2 rounded-lg bg-red-50/60 border border-red-100/70 px-3 py-2 text-[11px] text-red-600">
              <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span className="break-all">{service.reason}</span>
            </div>
          )}

          <div
            className={`grid transition-all duration-250 ${
              expanded ? "grid-rows-[1fr] opacity-100 mt-3" : "grid-rows-[0fr] opacity-0 mt-0"
            }`}
          >
            <div className="overflow-hidden">
              <div className="rounded-xl bg-black/[0.02] border border-black/[0.05] p-3 space-y-2">
                {meta.details.map((d) => (
                  <div key={d.label} className="flex items-start gap-2">
                    <span className="text-[10px] font-medium text-gray-400 uppercase tracking-wide w-16 shrink-0 pt-0.5">
                      {d.label}
                    </span>
                    <span className="text-[11px] text-gray-600">{d.value}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="mt-2 flex items-center justify-end">
            <span className="text-[10px] text-gray-400 flex items-center gap-0.5">
              <ChevronDown
                className={`w-3.5 h-3.5 transition-transform duration-200 ${
                  expanded ? "rotate-180" : ""
                }`}
              />
              {expanded ? "收起详情" : "查看详情"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function SummaryBar({
  services,
  loading,
  lastChecked,
  onRefresh,
}: {
  services: Capabilities;
  loading: boolean;
  lastChecked: string | null;
  onRefresh: () => void;
}) {
  const total = Object.keys(services).length;
  const online = Object.values(services).filter((s) => s.available).length;
  const healthy = online === total;

  return (
    <div className="bg-white/60 backdrop-blur-xl rounded-2xl border border-black/[0.06] p-4 mb-4 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div className="flex items-center gap-3">
        <div
          className={`w-10 h-10 rounded-xl flex items-center justify-center ${
            healthy ? "bg-emerald-50 text-emerald-600" : "bg-amber-50 text-amber-600"
          }`}
        >
          {healthy ? <CheckCircle2 className="w-5 h-5" /> : <AlertCircle className="w-5 h-5" />}
        </div>
        <div>
          <h3 className="text-[13px] font-semibold text-gray-800">
            {healthy ? "全部服务在线" : `${total - online} 项服务未就绪`}
          </h3>
          <p className="text-[11px] text-gray-500">
            {online}/{total} 在线 · {lastChecked ? `上次检测 ${lastChecked}` : "尚未检测"}
          </p>
        </div>
      </div>
      <button
        onClick={onRefresh}
        disabled={loading}
        className="self-start sm:self-auto flex items-center gap-1.5 px-3.5 py-2 text-[12px] font-medium rounded-xl bg-[#002fa7]/10 text-[#002fa7] hover:bg-[#002fa7]/15 active:scale-95 transition-all disabled:opacity-50 disabled:active:scale-100"
      >
        {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Activity className="w-3.5 h-3.5" />}
        {loading ? "检测中…" : "立即刷新"}
      </button>
    </div>
  );
}

export default function CapabilitiesStatus({ refreshIntervalMs = 30000 }: CapabilitiesStatusProps) {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<keyof Capabilities, boolean>>({
    ai_gateway: false,
    milvus: false,
    mineru: false,
  });

  const fetchCapabilities = useCallback(async () => {
    try {
      setError(null);
      setLoading(true);
      const data = await getCapabilities();
      setCapabilities(data);
      setLastChecked(
        new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "获取状态失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCapabilities();
    if (!refreshIntervalMs || refreshIntervalMs <= 0) return;
    const timer = setInterval(fetchCapabilities, refreshIntervalMs);
    return () => clearInterval(timer);
  }, [fetchCapabilities, refreshIntervalMs]);

  const toggleDetail = useCallback((key: keyof Capabilities) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  if (loading && !capabilities) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-gray-500 py-4">
        <Loader2 className="w-4 h-4 animate-spin" />
        正在检测后端服务状态…
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-white/60 backdrop-blur-xl rounded-2xl border border-black/[0.06] p-5">
        <div className="flex items-start gap-3 rounded-xl border border-red-100 bg-red-50/60 px-4 py-3.5 text-[12px] text-red-600">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <div>
            <p className="font-medium">无法获取服务状态</p>
            <p className="mt-0.5 text-red-500/80">{error}</p>
          </div>
        </div>
        <button
          onClick={fetchCapabilities}
          className="mt-3 text-[12px] font-medium text-[#002fa7] hover:underline"
        >
          重试
        </button>
      </div>
    );
  }

  if (!capabilities) return null;

  return (
    <div className="space-y-4">
      <SummaryBar
        services={capabilities}
        loading={loading}
        lastChecked={lastChecked}
        onRefresh={fetchCapabilities}
      />
      <div className="grid gap-3">
        {(Object.entries(capabilities) as [keyof Capabilities, CapabilityStatus][]).map(([key, status]) => (
          <ServiceCard
            key={key}
            serviceKey={key}
            service={status}
            expanded={expanded[key]}
            onToggle={() => toggleDetail(key)}
          />
        ))}
      </div>
      <p className="text-[11px] text-gray-500 leading-relaxed px-1">
        Core 模式无需这些服务即可运行；Full 模式会启用全部能力。Unavailable 的服务会自动降级。
      </p>
    </div>
  );
}
