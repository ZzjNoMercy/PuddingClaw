"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertCircle,
  BarChart3,
  BookOpenText,
  CheckCircle2,
  Database,
  FileSpreadsheet,
  Layers3,
  Loader2,
  RefreshCw,
  Sigma,
  Upload,
  type LucideIcon,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import {
  generateTableAssetProfile,
  listTableAssets,
  refreshTableAssetProfiles,
  type TableAsset,
} from "@/lib/api";
import { useApp } from "@/lib/store";

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    if (error.message === "Not Found" || error.message.includes("404")) {
      return "智能问数 API 暂时不可用，请重启 backend 让新路由生效。";
    }
    return error.message;
  }
  return String(error || "未知错误");
}

function sourceTypeLabel(asset: TableAsset): string {
  if (asset.source_type === "excel") return asset.sheet_name ? `Excel · ${asset.sheet_name}` : "Excel";
  if (asset.source_type === "csv") return "CSV";
  if (asset.source_type === "tsv") return "TSV";
  return asset.source_type;
}

export default function AnalyticsWorkbenchPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [assets, setAssets] = useState<TableAsset[]>([]);
  const [loading, setLoading] = useState(false);
  const [profilingAssetId, setProfilingAssetId] = useState<string | null>(null);
  const [refreshingProfiles, setRefreshingProfiles] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);

  useEffect(() => setMounted(true), []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setAssets(await listTableAssets(false));
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSidebarResize = useCallback(
    (delta: number) => {
      setSidebarWidth((prev: number) => Math.max(200, prev + delta));
    },
    [setSidebarWidth]
  );

  const readyCount = useMemo(() => assets.filter((asset) => asset.profile_status === "ready").length, [assets]);
  const missingCount = assets.length - readyCount;

  const generateOneProfile = useCallback(
    async (asset: TableAsset) => {
      setProfilingAssetId(asset.asset_id);
      try {
        const updated = await generateTableAssetProfile(asset.asset_id);
        setAssets((current) => current.map((item) => (item.asset_id === updated.asset_id ? updated : item)));
        setToast({ type: "success", message: "Profile 已生成" });
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setProfilingAssetId(null);
      }
    },
    []
  );

  const generateAllProfiles = useCallback(async () => {
    setRefreshingProfiles(true);
    try {
      await refreshTableAssetProfiles();
      setAssets(await listTableAssets(false));
      setToast({ type: "success", message: "表格 Profile 已刷新" });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setRefreshingProfiles(false);
    }
  }, []);

  return (
    <div className="h-screen app-bg text-gray-900">
      <div className="fixed left-3 top-3 z-[80]">
        <Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact />
      </div>

      <div className="flex h-full overflow-hidden">
        <div
          className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden"
          style={{ width: sidebarOpen ? sidebarWidth : 0 }}
        >
          <div style={{ width: sidebarWidth, minWidth: 200 }} className="h-full flex flex-col">
            <div className="h-11 shrink-0" />
            <div className="flex-1 min-h-0 overflow-hidden">
              <Sidebar />
            </div>
          </div>
        </div>

        {mounted && sidebarOpen && <ResizeHandle onResize={handleSidebarResize} direction="left" />}

        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto">
            <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 px-5 py-6">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h1 className="text-2xl font-semibold tracking-tight text-gray-950">智能问数</h1>
                  <p className="mt-2 max-w-3xl text-sm leading-6 text-gray-500">
                    管理 Excel / CSV / TSV 表格资产、生成 Profile，并为后续数据模型、指标口径和专门问数 Agent 做准备。
                    文件上传仍统一在知识库入口完成。
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Link
                    href="/knowledge"
                    className="inline-flex h-10 items-center gap-2 rounded-full bg-[#002fa7]/10 px-4 text-sm font-medium text-[#002fa7] transition hover:bg-[#002fa7]/15"
                  >
                    <Upload className="h-4 w-4" />
                    上传文件
                  </Link>
                  <button
                    type="button"
                    onClick={refresh}
                    disabled={loading}
                    className="inline-flex h-10 items-center gap-2 rounded-full border border-black/[0.08] bg-white px-4 text-sm font-medium text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
                  >
                    <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
                    刷新
                  </button>
                </div>
              </div>

              {toast ? (
                <div
                  className={`flex items-start gap-2 rounded-2xl border px-4 py-3 text-sm ${
                    toast.type === "success"
                      ? "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                      : "border-red-500/15 bg-red-50 text-red-600"
                  }`}
                >
                  {toast.type === "success" ? <CheckCircle2 className="mt-0.5 h-4 w-4" /> : <AlertCircle className="mt-0.5 h-4 w-4" />}
                  <span className="break-all">{toast.message}</span>
                </div>
              ) : null}

              <section className="grid gap-4 md:grid-cols-4">
                <MetricCard icon={FileSpreadsheet} title="表格资产" value={assets.length} tone="blue" />
                <MetricCard icon={CheckCircle2} title="Profile 可用" value={readyCount} tone="green" />
                <MetricCard icon={AlertCircle} title="待生成" value={missingCount} tone="orange" />
                <MetricCard icon={BarChart3} title="问数工具" value="Pandas" tone="blue" />
              </section>

              <section className="grid gap-5 lg:grid-cols-[1fr_320px]">
                <div className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <h2 className="text-lg font-semibold text-gray-950">表格资产 Catalog</h2>
                      <p className="mt-1 text-sm text-gray-500">
                        来自知识库统一上传入口；这里只做识别、Profile 和问数资产管理。
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={generateAllProfiles}
                      disabled={refreshingProfiles || assets.length === 0}
                      className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      {refreshingProfiles ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
                      生成全部 Profile
                    </button>
                  </div>

                  <div className="mt-5 space-y-3">
                    {loading ? (
                      <div className="flex items-center justify-center rounded-3xl border border-dashed border-black/[0.08] py-16 text-sm text-gray-400">
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        正在读取表格资产…
                      </div>
                    ) : assets.length === 0 ? (
                      <div className="rounded-3xl border border-dashed border-black/[0.08] px-5 py-12 text-center">
                        <p className="text-sm font-medium text-gray-700">还没有识别到 Excel / CSV / TSV。</p>
                        <p className="mt-2 text-sm text-gray-400">先去知识库上传文件，导入完成后这里会自动出现。</p>
                        <Link
                          href="/knowledge"
                          className="mt-5 inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white"
                        >
                          <Upload className="h-4 w-4" />
                          去上传
                        </Link>
                      </div>
                    ) : (
                      assets.map((asset) => (
                        <article
                          key={asset.asset_id}
                          className="rounded-3xl border border-black/[0.06] bg-white px-4 py-4 shadow-sm transition hover:border-[#002fa7]/20 hover:bg-[#002fa7]/[0.015]"
                        >
                          <div className="flex items-start justify-between gap-4">
                            <div className="min-w-0">
                              <div className="flex flex-wrap items-center gap-2">
                                <FileSpreadsheet className="h-4 w-4 text-[#002fa7]" />
                                <h3 className="truncate text-sm font-semibold text-gray-950" title={asset.file_name}>
                                  {asset.file_name}
                                </h3>
                                <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-medium text-[#002fa7]">
                                  {sourceTypeLabel(asset)}
                                </span>
                                <span
                                  className={`rounded-full px-2.5 py-1 text-xs font-medium ${
                                    asset.profile_status === "ready"
                                      ? "bg-emerald-50 text-emerald-700"
                                      : "bg-orange-50 text-orange-700"
                                  }`}
                                >
                                  {asset.profile_status === "ready" ? "Profile 可用" : "待生成 Profile"}
                                </span>
                              </div>
                              <p className="mt-2 break-all text-xs text-gray-400">{asset.virtual_path}</p>
                              <div className="mt-3 flex flex-wrap gap-2 text-xs text-gray-500">
                                <span>{formatBytes(asset.size_bytes)}</span>
                                {typeof asset.rows === "number" ? <span>{asset.rows} 行</span> : null}
                                {typeof asset.columns_count === "number" ? <span>{asset.columns_count} 列</span> : null}
                                {asset.columns?.length ? <span>字段：{asset.columns.slice(0, 5).join("、")}</span> : null}
                              </div>
                            </div>
                            <button
                              type="button"
                              onClick={() => generateOneProfile(asset)}
                              disabled={profilingAssetId === asset.asset_id}
                              className="shrink-0 rounded-2xl border border-black/[0.08] bg-white px-3.5 py-2 text-xs font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
                            >
                              {profilingAssetId === asset.asset_id ? "生成中" : asset.profile_status === "ready" ? "重新生成" : "生成"}
                            </button>
                          </div>
                        </article>
                      ))
                    )}
                  </div>
                </div>

                <aside className="flex flex-col gap-4">
                  <WorkbenchCard
                    icon={Layers3}
                    title="数据模型"
                    description="把多个表格、数据库表组合成一个可问数的数据模型。下一步会支持选择数据源、主键、关联关系。"
                  />
                  <WorkbenchCard
                    icon={Sigma}
                    title="指标口径"
                    description="指标是全局语义资产，不挂在单个 Excel 下；它会声明适用模型、字段需求、自然语言定义和公式。"
                  />
                  <WorkbenchCard
                    icon={BookOpenText}
                    title="问数 Agent"
                    description="专门对话入口会先读取模型、指标和 profile，再选择 Pandas 或后续 Vanna/NL2SQL。"
                  />
                </aside>
              </section>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}

function MetricCard({
  icon: Icon,
  title,
  value,
  tone,
}: {
  icon: LucideIcon;
  title: string;
  value: string | number;
  tone: "blue" | "green" | "orange";
}) {
  const styles = {
    blue: "bg-[#002fa7]/[0.06] text-[#002fa7]",
    green: "bg-emerald-50 text-emerald-700",
    orange: "bg-orange-50 text-orange-700",
  }[tone];
  return (
    <div className="rounded-[24px] border border-black/[0.06] bg-white p-4 shadow-sm">
      <div className={`flex h-10 w-10 items-center justify-center rounded-2xl ${styles}`}>
        <Icon className="h-5 w-5" />
      </div>
      <p className="mt-4 text-xs font-medium text-gray-400">{title}</p>
      <p className="mt-1 text-2xl font-semibold text-gray-950">{value}</p>
    </div>
  );
}

function WorkbenchCard({
  icon: Icon,
  title,
  description,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  return (
    <div className="rounded-[24px] border border-black/[0.06] bg-white p-5 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-[#002fa7]/[0.06] text-[#002fa7]">
          <Icon className="h-5 w-5" />
        </div>
        <h3 className="font-semibold text-gray-950">{title}</h3>
      </div>
      <p className="mt-3 text-sm leading-6 text-gray-500">{description}</p>
    </div>
  );
}
