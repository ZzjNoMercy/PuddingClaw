"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  AlertCircle,
  BookOpenText,
  Bot,
  CheckCircle2,
  Database,
  FileSpreadsheet,
  Layers3,
  Loader2,
  RefreshCw,
  Sigma,
  Upload,
  X,
  type LucideIcon,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import {
  generateTableAssetProfile,
  getTableAsset,
  deleteKnowledgeDatabaseSourceVannaEntity,
  deleteKnowledgeDatabaseSourceVannaTraining,
  importKnowledgeDatabaseSourceVannaEntities,
  listTableAssetEntityCandidates,
  listKnowledgeDatabaseSourceVannaEntities,
  listKnowledgeDatabaseSourceVannaEntityCandidates,
  listKnowledgeDatabaseSourceVannaTraining,
  listKnowledgeDatabaseSourceTables,
  listKnowledgeDatabaseSources,
  listTableAssets,
  refreshTableAssetProfiles,
  saveKnowledgeDatabaseSource,
  testKnowledgeDatabaseSource,
  trainKnowledgeDatabaseSourceVanna,
  type KnowledgeDatabaseSource,
  type TableAsset,
  type TableEntityCandidate,
  type VannaEntityRecord,
  type VannaTrainingData,
  type VannaTrainingRecord,
} from "@/lib/api";
import { useApp } from "@/lib/store";

type AnalyticsSection = "assets" | "models" | "measures" | "agent";

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

function emptyDatabaseSource(): KnowledgeDatabaseSource {
  return {
    id: "",
    type: "postgresql",
    name: "",
    description: "",
    host: "127.0.0.1",
    port: 5432,
    database: "puddingclaw",
    username: "puddingclaw",
    password: "",
    selected_tables: [],
  };
}

export default function AnalyticsWorkbenchPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [assets, setAssets] = useState<TableAsset[]>([]);
  const [databaseSources, setDatabaseSources] = useState<KnowledgeDatabaseSource[]>([]);
  const [loading, setLoading] = useState(false);
  const [profilingAssetId, setProfilingAssetId] = useState<string | null>(null);
  const [refreshingProfiles, setRefreshingProfiles] = useState(false);
  const [profileAsset, setProfileAsset] = useState<TableAsset | null>(null);
  const [profileLoadingId, setProfileLoadingId] = useState<string | null>(null);
  const [showRawProfileJson, setShowRawProfileJson] = useState(false);
  const [entityCandidates, setEntityCandidates] = useState<TableEntityCandidate[]>([]);
  const [entityCandidatesLoading, setEntityCandidatesLoading] = useState(false);
  const [databaseModalOpen, setDatabaseModalOpen] = useState(false);
  const [databaseDraft, setDatabaseDraft] = useState<KnowledgeDatabaseSource>(() => emptyDatabaseSource());
  const [databaseTables, setDatabaseTables] = useState<string[]>([]);
  const [databaseBusy, setDatabaseBusy] = useState(false);
  const [databaseModalStatus, setDatabaseModalStatus] = useState<{ type: "success" | "error" | "info"; message: string } | null>(null);
  const [vannaTrainingTarget, setVannaTrainingTarget] = useState<{ source: KnowledgeDatabaseSource; table: string } | null>(null);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [activeSection, setActiveSection] = useState<AnalyticsSection>("assets");

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [nextAssets, nextDatabaseSources] = await Promise.all([
        listTableAssets(false),
        listKnowledgeDatabaseSources(),
      ]);
      setAssets(nextAssets);
      setDatabaseSources(nextDatabaseSources);
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
  const totalDataAssets = assets.length + databaseSources.length;

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
      const [nextAssets, nextDatabaseSources] = await Promise.all([
        listTableAssets(false),
        listKnowledgeDatabaseSources(),
      ]);
      setAssets(nextAssets);
      setDatabaseSources(nextDatabaseSources);
      setToast({ type: "success", message: "Profile 已刷新" });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setRefreshingProfiles(false);
    }
  }, []);

  const openProfile = useCallback(async (asset: TableAsset) => {
    if (asset.profile_status !== "ready") {
      setToast({ type: "error", message: "这个表还没有生成 Profile。" });
      return;
    }
    setProfileLoadingId(asset.asset_id);
    setShowRawProfileJson(false);
    setEntityCandidates([]);
    setEntityCandidatesLoading(true);
    try {
      const [fullAsset, candidates] = await Promise.all([
        getTableAsset(asset.asset_id, true),
        listTableAssetEntityCandidates(asset.asset_id),
      ]);
      setProfileAsset(fullAsset);
      setEntityCandidates(candidates);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setProfileLoadingId(null);
      setEntityCandidatesLoading(false);
    }
  }, []);

  const openDatabaseSourceModal = useCallback((source: KnowledgeDatabaseSource) => {
    setDatabaseDraft({ ...source, password: "" });
    setDatabaseTables(source.selected_tables ?? []);
    setDatabaseModalStatus(null);
    setDatabaseModalOpen(true);
  }, []);

  const openVannaTrainingModal = useCallback((source: KnowledgeDatabaseSource, table: string) => {
    setVannaTrainingTarget({ source, table });
  }, []);

  const updateDatabaseDraft = useCallback((updates: Partial<KnowledgeDatabaseSource>) => {
    setDatabaseDraft((current) => ({ ...current, ...updates }));
  }, []);

  const loadDatabaseTables = useCallback(async () => {
    if (!databaseDraft.id) {
      setDatabaseModalStatus({ type: "error", message: "请先保存数据源，再读取表。" });
      return;
    }
    setDatabaseBusy(true);
    setDatabaseModalStatus(null);
    try {
      const tables = await listKnowledgeDatabaseSourceTables(databaseDraft.id);
      setDatabaseTables(tables);
      updateDatabaseDraft({
        selected_tables: databaseDraft.selected_tables.length > 0 ? databaseDraft.selected_tables : tables,
      });
      setDatabaseModalStatus({ type: "success", message: `读取到 ${tables.length} 张表。` });
    } catch (error) {
      setDatabaseModalStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setDatabaseBusy(false);
    }
  }, [databaseDraft.id, databaseDraft.selected_tables, updateDatabaseDraft]);

  const testDatabaseDraft = useCallback(async () => {
    setDatabaseBusy(true);
    setDatabaseModalStatus(null);
    try {
      const result = await testKnowledgeDatabaseSource(databaseDraft);
      setDatabaseModalStatus({
        type: result.ok ? "success" : "error",
        message: result.message || (result.ok ? "连接成功" : "连接失败"),
      });
    } catch (error) {
      setDatabaseModalStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setDatabaseBusy(false);
    }
  }, [databaseDraft]);

  const saveDatabaseDraft = useCallback(async () => {
    setDatabaseBusy(true);
    setDatabaseModalStatus(null);
    try {
      const saved = await saveKnowledgeDatabaseSource({
        ...databaseDraft,
        selected_tables: databaseDraft.selected_tables,
      });
      setDatabaseModalOpen(false);
      setDatabaseDraft(emptyDatabaseSource());
      setDatabaseTables([]);
      setToast({ type: "success", message: `已保存数据源：${saved.name}` });
      setDatabaseSources(await listKnowledgeDatabaseSources());
    } catch (error) {
      setDatabaseModalStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setDatabaseBusy(false);
    }
  }, [databaseDraft]);

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
            <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 px-5 py-6">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h1 className="text-2xl font-semibold tracking-tight text-gray-950">智能问数</h1>
                  <p className="mt-2 max-w-3xl text-sm leading-6 text-gray-500">
                    管理 Excel / CSV / TSV、数据库源等数据资产，生成 Profile，并为后续数据模型、度量值和专门问数 Agent 做准备。
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

              <section className="grid min-h-[620px] gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
                <aside className="rounded-[26px] border border-black/[0.06] bg-white p-3 shadow-sm lg:sticky lg:top-6 lg:h-fit">
                  <div className="px-3 py-3">
                    <p className="text-xs font-semibold uppercase tracking-[0.18em] text-gray-400">问数工作台</p>
                    <p className="mt-1 text-sm text-gray-500">资产、模型、度量值和问数入口放在同一层管理。</p>
                  </div>
                  <nav className="mt-1 space-y-1">
                    <AnalyticsNavButton
                      active={activeSection === "assets"}
                      icon={Database}
                      title="数据资产"
                      description={`${totalDataAssets} 个资产`}
                      onClick={() => setActiveSection("assets")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "models"}
                      icon={Layers3}
                      title="数据模型"
                      description="模型 reference"
                      onClick={() => setActiveSection("models")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "measures"}
                      icon={Sigma}
                      title="度量值"
                      description="指标口径"
                      onClick={() => setActiveSection("measures")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "agent"}
                      icon={Bot}
                      title="问数 Agent"
                      description="专门对话"
                      onClick={() => setActiveSection("agent")}
                    />
                  </nav>
                </aside>

                <div className="min-w-0 rounded-[28px] border border-black/[0.06] bg-white shadow-sm">
                  {activeSection === "assets" ? (
                    <section className="p-5">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <h2 className="text-lg font-semibold text-gray-950">数据资产</h2>
                          <p className="mt-1 text-sm text-gray-500">
                            表格文件和数据库源统一在这里管理；Profile 是问数 Agent 理解字段的机器画像。
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

                      <div className="mt-4 grid gap-3 md:grid-cols-4">
                        <MetricCard icon={Database} title="数据资产" value={totalDataAssets} tone="blue" compact />
                        <MetricCard icon={FileSpreadsheet} title="表格文件" value={assets.length} tone="blue" compact />
                        <MetricCard icon={CheckCircle2} title="Profile 可用" value={readyCount} tone="green" compact />
                        <MetricCard icon={AlertCircle} title="待生成" value={missingCount} tone="orange" compact />
                      </div>

                      <div className="mt-5 space-y-4">
                        {loading ? (
                          <div className="flex items-center justify-center rounded-3xl border border-dashed border-black/[0.08] py-16 text-sm text-gray-400">
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            正在读取数据资产…
                          </div>
                        ) : totalDataAssets === 0 ? (
                          <div className="rounded-3xl border border-dashed border-black/[0.08] px-5 py-12 text-center">
                            <p className="text-sm font-medium text-gray-700">还没有识别到数据资产。</p>
                            <p className="mt-2 text-sm text-gray-400">先去知识库上传表格文件，或配置数据库源。</p>
                            <Link
                              href="/knowledge"
                              className="mt-5 inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white"
                            >
                              <Upload className="h-4 w-4" />
                              去上传
                            </Link>
                          </div>
                        ) : (
                          <>
                            {databaseSources.length ? (
                              <section className="space-y-2">
                                <div className="flex items-center justify-between px-1">
                                  <h3 className="text-xs font-semibold uppercase tracking-[0.18em] text-gray-400">数据库源</h3>
                                  <span className="text-xs text-gray-400">{databaseSources.length} 个</span>
                                </div>
                                {databaseSources.map((source) => (
                                  <DatabaseSourceCard key={source.id} source={source} onManage={openDatabaseSourceModal} />
                                ))}
                              </section>
                            ) : null}

                            {assets.length ? (
                              <section className="space-y-2">
                                <div className="flex items-center justify-between px-1 pt-2">
                                  <h3 className="text-xs font-semibold uppercase tracking-[0.18em] text-gray-400">表格文件</h3>
                                  <span className="text-xs text-gray-400">{assets.length} 个</span>
                                </div>
                                {assets.map((asset) => (
                                  <TableAssetCard
                                    key={asset.asset_id}
                                    asset={asset}
                                    profileLoadingId={profileLoadingId}
                                    profilingAssetId={profilingAssetId}
                                    onOpenProfile={openProfile}
                                    onGenerateProfile={generateOneProfile}
                                  />
                                ))}
                              </section>
                            ) : null}
                          </>
                        )}
                      </div>
                    </section>
                  ) : null}

                  {activeSection === "models" ? (
                    <WorkbenchSection
                      icon={Layers3}
                      title="数据模型"
                      subtitle="模型是面向问数的业务包：选择数据资产，再写一份像 reference 一样的自然语言说明。"
                      primaryAction="新建数据模型"
                    >
                      <EmptyWorkbenchState
                        title="还没有数据模型"
                        description="下一步会把数据源选择、模型 reference 和默认字段提示放到这里。模型不是传统关系建模器，而是 AI Native BI 的业务上下文。"
                      />
                    </WorkbenchSection>
                  ) : null}

                  {activeSection === "measures" ? (
                    <WorkbenchSection
                      icon={Sigma}
                      title="度量值"
                      subtitle="度量值是顶层 BI 资产，保存自然语言口径、公式 hint、字段需求和示例问法，可被多个模型复用。"
                      primaryAction="新建度量值"
                    >
                      <EmptyWorkbenchState
                        title="还没有度量值"
                        description="例如：周销量、环比、配置率。后续这里会支持粘贴 reference 文档，问数 Agent 会像读取 skill reference 一样读取它。"
                      />
                    </WorkbenchSection>
                  ) : null}

                  {activeSection === "agent" ? (
                    <WorkbenchSection
                      icon={Bot}
                      title="问数 Agent"
                      subtitle="专门的问数入口会读取选中的数据模型、度量值和表格 Profile，再选择 Pandas 或后续 Vanna/NL2SQL。"
                      primaryAction="开始问数"
                    >
                      <EmptyWorkbenchState
                        title="专门问数 Agent 待接入"
                        description="这里后续会接独立 session。普通 Agent 仍可通过工具调用问数能力，但这个入口会更像一个 BI 分析工作台。"
                      />
                    </WorkbenchSection>
                  ) : null}
                </div>
              </section>
            </div>
          </div>
        </main>
      </div>

      {profileAsset?.profile ? (
        <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
          <div className="flex max-h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
            <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
              <div className="min-w-0">
                <h3 className="truncate text-lg font-semibold text-gray-950">{profileAsset.file_name}</h3>
                <p className="mt-1 text-sm text-gray-500">
                  Profile · {sourceTypeLabel(profileAsset)} · {profileAsset.profile.shape?.[0] ?? "-"} 行 ·{" "}
                  {profileAsset.profile.shape?.[1] ?? "-"} 列
                </p>
              </div>
              <button
                type="button"
                onClick={() => setProfileAsset(null)}
                className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900"
                aria-label="关闭"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-5">
              <div className="mb-5 rounded-3xl border border-[#002fa7]/10 bg-[#002fa7]/[0.035] p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h4 className="text-sm font-semibold text-gray-950">实体候选</h4>
                    <p className="mt-1 text-xs leading-5 text-gray-500">
                      系统只根据字段画像推荐可能适合作为标准值字典的列；这不是业务规则，也不会直接进入 Vanna。Vanna 训练只面向数据库源表。
                    </p>
                  </div>
                  <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-[#002fa7] shadow-sm ring-1 ring-[#002fa7]/10">
                    {entityCandidates.length} 个候选
                  </span>
                </div>
                <EntityCandidateList
                  candidates={entityCandidates}
                  loading={entityCandidatesLoading}
                />
              </div>

              <div className="grid gap-4 md:grid-cols-3">
                <div className="rounded-3xl bg-[#002fa7]/[0.05] p-4">
                  <p className="text-xs font-medium text-gray-500">行数</p>
                  <p className="mt-2 text-2xl font-semibold text-[#002fa7]">{profileAsset.profile.shape?.[0] ?? "-"}</p>
                </div>
                <div className="rounded-3xl bg-emerald-50 p-4">
                  <p className="text-xs font-medium text-gray-500">列数</p>
                  <p className="mt-2 text-2xl font-semibold text-emerald-700">{profileAsset.profile.shape?.[1] ?? "-"}</p>
                </div>
                <div className="rounded-3xl bg-orange-50 p-4">
                  <p className="text-xs font-medium text-gray-500">生成时间</p>
                  <p className="mt-2 truncate text-sm font-semibold text-orange-700">
                    {profileAsset.profile.generated_at ? new Date(profileAsset.profile.generated_at).toLocaleString("zh-CN", { hour12: false }) : "-"}
                  </p>
                </div>
              </div>

              <section className="mt-5 rounded-3xl border border-black/[0.06] p-4">
                <h4 className="text-sm font-semibold text-gray-950">字段画像</h4>
                <div className="mt-3 overflow-x-auto">
                  <table className="min-w-full text-left text-xs">
                    <thead className="text-gray-400">
                      <tr>
                        <th className="whitespace-nowrap px-3 py-2 font-semibold">字段</th>
                        <th className="whitespace-nowrap px-3 py-2 font-semibold">类型</th>
                        <th className="whitespace-nowrap px-3 py-2 font-semibold">非空</th>
                        <th className="whitespace-nowrap px-3 py-2 font-semibold">空值</th>
                        <th className="whitespace-nowrap px-3 py-2 font-semibold">唯一值</th>
                        <th className="px-3 py-2 font-semibold">样例</th>
                        <th className="whitespace-nowrap px-3 py-2 font-semibold">角色</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-black/[0.04]">
                      {(profileAsset.profile.columns || []).map((column) => (
                        <tr key={column.name} className="align-top">
                          <td className="whitespace-nowrap px-3 py-2 font-medium text-gray-900">{column.name}</td>
                          <td className="whitespace-nowrap px-3 py-2 text-gray-500">{column.dtype}</td>
                          <td className="whitespace-nowrap px-3 py-2 text-gray-500">{column.non_null ?? "-"}</td>
                          <td className="whitespace-nowrap px-3 py-2 text-gray-500">{column.null_count ?? "-"}</td>
                          <td className="whitespace-nowrap px-3 py-2 text-gray-500">
                            {typeof column.distinct_count === "number"
                              ? `${column.distinct_count}${typeof column.distinct_ratio === "number" ? ` · ${(column.distinct_ratio * 100).toFixed(1)}%` : ""}`
                              : "-"}
                          </td>
                          <td className="px-3 py-2 text-gray-500">
                            {(column.sample_values || []).slice(0, 5).join("、") || "-"}
                          </td>
                          <td className="whitespace-nowrap px-3 py-2 text-gray-500">{column.semantic_role_hint || "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="mt-5 rounded-3xl border border-black/[0.06] p-4">
                <h4 className="text-sm font-semibold text-gray-950">数据预览</h4>
                <div className="mt-3 overflow-x-auto">
                  <table className="min-w-full text-left text-xs">
                    <thead className="text-gray-400">
                      <tr>
                        {Object.keys(profileAsset.profile.preview?.[0] || {}).map((key) => (
                          <th key={key} className="whitespace-nowrap px-3 py-2 font-semibold">{key}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-black/[0.04]">
                      {(profileAsset.profile.preview || []).map((row, index) => (
                        <tr key={index}>
                          {Object.keys(profileAsset.profile?.preview?.[0] || {}).map((key) => (
                            <td key={key} className="max-w-[220px] truncate px-3 py-2 text-gray-600" title={String(row[key] ?? "")}>
                              {String(row[key] ?? "")}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!profileAsset.profile.preview?.length ? (
                    <p className="py-8 text-center text-xs text-gray-400">没有预览数据。</p>
                  ) : null}
                </div>
              </section>

              <section className="mt-5 rounded-3xl border border-black/[0.06] p-4">
                <button
                  type="button"
                  onClick={() => setShowRawProfileJson((value) => !value)}
                  className="text-sm font-semibold text-[#002fa7]"
                >
                  {showRawProfileJson ? "收起原始 JSON" : "查看原始 JSON"}
                </button>
                {showRawProfileJson ? (
                  <pre className="mt-3 max-h-80 overflow-auto rounded-2xl bg-gray-950 p-4 text-xs leading-5 text-gray-100">
                    {JSON.stringify(profileAsset.profile, null, 2)}
                  </pre>
                ) : null}
              </section>
            </div>
          </div>
        </div>
      ) : null}

      {databaseModalOpen ? (
        <DatabaseSourceModal
          draft={databaseDraft}
          tables={databaseTables}
          busy={databaseBusy}
          status={databaseModalStatus}
          onClose={() => setDatabaseModalOpen(false)}
          onUpdate={updateDatabaseDraft}
          onLoadTables={loadDatabaseTables}
          onTest={testDatabaseDraft}
          onSave={saveDatabaseDraft}
        />
      ) : null}
    </div>
  );
}

function MetricCard({
  icon: Icon,
  title,
  value,
  tone,
  compact = false,
}: {
  icon: LucideIcon;
  title: string;
  value: string | number;
  tone: "blue" | "green" | "orange";
  compact?: boolean;
}) {
  const styles = {
    blue: "bg-[#002fa7]/[0.06] text-[#002fa7]",
    green: "bg-emerald-50 text-emerald-700",
    orange: "bg-orange-50 text-orange-700",
  }[tone];
  return (
    <div className={`${compact ? "rounded-2xl p-3" : "rounded-[24px] p-4"} border border-black/[0.06] bg-white shadow-sm`}>
      <div className={`flex ${compact ? "h-8 w-8 rounded-xl" : "h-10 w-10 rounded-2xl"} items-center justify-center ${styles}`}>
        <Icon className="h-5 w-5" />
      </div>
      <p className={`${compact ? "mt-3" : "mt-4"} text-xs font-medium text-gray-400`}>{title}</p>
      <p className={`${compact ? "text-xl" : "text-2xl"} mt-1 font-semibold text-gray-950`}>{value}</p>
    </div>
  );
}

function EntityCandidateList({ candidates, loading }: { candidates: TableEntityCandidate[]; loading: boolean }) {
  if (loading) {
    return (
      <div className="mt-4 flex items-center justify-center rounded-2xl border border-dashed border-[#002fa7]/15 bg-white/70 py-8 text-xs text-gray-400">
        <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
        正在分析哪些字段适合做实体字典…
      </div>
    );
  }

  if (candidates.length === 0) {
    return (
      <div className="mt-4 rounded-2xl border border-dashed border-[#002fa7]/15 bg-white/70 px-4 py-6 text-center text-xs text-gray-400">
        暂未发现明显实体候选。你仍然可以在数据模型 reference 里手动说明这些字段的业务含义。
      </div>
    );
  }

  return (
    <div className="mt-4 space-y-3">
      <div className="rounded-2xl border border-amber-500/15 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-800">
        这里的候选只服务于表格 Profile 和后续数据模型 reference，不会直接写入 Vanna。Vanna 只处理已配置的数据库源和数据库表。
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {candidates.map((candidate) => {
          return (
            <article
              key={candidate.column}
              className="rounded-2xl border border-black/[0.06] bg-white p-3 transition hover:border-[#002fa7]/20"
            >
              <div className="flex flex-wrap items-center gap-2">
                <h5 className="truncate text-sm font-semibold text-gray-950" title={candidate.column}>
                  {candidate.column}
                </h5>
                <span className="rounded-full bg-[#002fa7]/10 px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">
                  推荐度 {Math.round((candidate.score || 0) * 100)}%
                </span>
                <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
                  建议标签：{candidate.suggested_entity_type}
                </span>
                {candidate.dtype ? (
                  <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
                    {candidate.dtype}
                  </span>
                ) : null}
              </div>

              <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-gray-500">
                {typeof candidate.distinct_count === "number" ? <span>唯一值 {candidate.distinct_count}</span> : null}
                {typeof candidate.distinct_ratio === "number" ? (
                  <span>占比 {(candidate.distinct_ratio * 100).toFixed(1)}%</span>
                ) : null}
                {candidate.table_column ? <span className="truncate">字段 {candidate.table_column}</span> : null}
              </div>

              {candidate.sample_values?.length ? (
                <p className="mt-2 line-clamp-2 text-xs text-gray-500">
                  样例：{candidate.sample_values.slice(0, 6).join("、")}
                </p>
              ) : null}

              {candidate.reasons?.length ? (
                <p className="mt-2 line-clamp-2 text-[11px] text-gray-400">
                  推荐原因：{candidate.reasons.join("；")}
                </p>
              ) : null}
            </article>
          );
        })}
      </div>
    </div>
  );
}

function AnalyticsNavButton({
  active,
  icon: Icon,
  title,
  description,
  onClick,
}: {
  active: boolean;
  icon: LucideIcon;
  title: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-center gap-3 rounded-2xl px-3 py-3 text-left transition ${
        active
          ? "bg-[#002fa7] text-white shadow-sm"
          : "text-gray-600 hover:bg-[#002fa7]/[0.06] hover:text-[#002fa7]"
      }`}
    >
      <span
        className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${
          active ? "bg-white/15 text-white" : "bg-[#002fa7]/[0.06] text-[#002fa7]"
        }`}
      >
        <Icon className="h-4.5 w-4.5" />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold">{title}</span>
        <span className={`mt-0.5 block truncate text-xs ${active ? "text-white/70" : "text-gray-400"}`}>{description}</span>
      </span>
    </button>
  );
}

function WorkbenchSection({
  icon: Icon,
  title,
  subtitle,
  primaryAction,
  children,
}: {
  icon: LucideIcon;
  title: string;
  subtitle: string;
  primaryAction: string;
  children: ReactNode;
}) {
  return (
    <section className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-black/[0.06] pb-5">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.06] text-[#002fa7]">
            <Icon className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-gray-950">{title}</h2>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-gray-500">{subtitle}</p>
          </div>
        </div>
        <button
          type="button"
          className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-[#001f7a]"
        >
          <PlusIcon />
          {primaryAction}
        </button>
      </div>
      <div className="mt-5">{children}</div>
    </section>
  );
}

function EmptyWorkbenchState({ title, description }: { title: string; description: string }) {
  return (
    <div className="rounded-3xl border border-dashed border-black/[0.08] bg-black/[0.015] px-6 py-14 text-center">
      <p className="text-sm font-semibold text-gray-800">{title}</p>
      <p className="mx-auto mt-2 max-w-xl text-sm leading-6 text-gray-500">{description}</p>
    </div>
  );
}

function PlusIcon() {
  return <span className="text-lg leading-none">+</span>;
}

function DatabaseSourceCard({
  source,
  onManage,
}: {
  source: KnowledgeDatabaseSource;
  onManage: (source: KnowledgeDatabaseSource) => void;
}) {
  const tableCount = source.selected_tables?.length ?? 0;
  return (
    <article className="rounded-3xl border border-black/[0.06] bg-white px-4 py-4 shadow-sm transition hover:border-[#002fa7]/20 hover:bg-[#002fa7]/[0.015]">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Database className="h-4 w-4 text-[#002fa7]" />
            <h3 className="truncate text-sm font-semibold text-gray-950" title={source.name}>
              {source.name}
            </h3>
            <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-medium text-[#002fa7]">
              PostgreSQL
            </span>
            {source.builtin ? (
              <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700">
                项目默认
              </span>
            ) : null}
          </div>
          <p className="mt-2 break-all text-xs text-gray-400">
            {source.username}@{source.host}:{source.port}/{source.database}
          </p>
          <div className="mt-3 flex flex-wrap gap-2 text-xs text-gray-500">
            <span>{tableCount > 0 ? `${tableCount} 张已选择表` : "未选择表"}</span>
            {source.password_configured ? <span>密码已配置</span> : <span>密码未保存</span>}
            {source.description ? <span className="max-w-[520px] truncate">{source.description}</span> : null}
          </div>
        </div>
        <button
          type="button"
          onClick={() => onManage(source)}
          className="shrink-0 rounded-2xl border border-black/[0.08] bg-white px-3.5 py-2 text-xs font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50"
        >
          管理
        </button>
      </div>
    </article>
  );
}

function DatabaseSourceModal({
  draft,
  tables,
  busy,
  status,
  onClose,
  onUpdate,
  onLoadTables,
  onTest,
  onSave,
}: {
  draft: KnowledgeDatabaseSource;
  tables: string[];
  busy: boolean;
  status: { type: "success" | "error" | "info"; message: string } | null;
  onClose: () => void;
  onUpdate: (updates: Partial<KnowledgeDatabaseSource>) => void;
  onLoadTables: () => void;
  onTest: () => void;
  onSave: () => void;
}) {
  const isProjectDefault = draft.id === "project_postgres";
  const [trainingData, setTrainingData] = useState<VannaTrainingData | null>(null);
  const [trainingLoading, setTrainingLoading] = useState(false);
  const [trainingBusy, setTrainingBusy] = useState(false);
  const [trainingStatus, setTrainingStatus] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [documentation, setDocumentation] = useState("");
  const [sqlQuestion, setSqlQuestion] = useState("");
  const [sqlExample, setSqlExample] = useState("");
  const [entityTable, setEntityTable] = useState(draft.selected_tables[0] || "");
  const [entityCandidates, setEntityCandidates] = useState<TableEntityCandidate[]>([]);
  const [entityRecords, setEntityRecords] = useState<VannaEntityRecord[]>([]);
  const [entityColumn, setEntityColumn] = useState("");
  const [entityType, setEntityType] = useState("");
  const [entityAliasColumns, setEntityAliasColumns] = useState("");
  const [entityMaxValues, setEntityMaxValues] = useState(1000);

  const refreshTrainingData = useCallback(async () => {
    if (!draft.id || !entityTable) return;
    setTrainingLoading(true);
    try {
      const data = await listKnowledgeDatabaseSourceVannaTraining(draft.id, entityTable);
      setTrainingData(data);
    } catch (error) {
      setTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingLoading(false);
    }
  }, [draft.id, entityTable]);

  useEffect(() => {
    void refreshTrainingData();
  }, [refreshTrainingData]);

  useEffect(() => {
    if (draft.selected_tables.length > 0 && (!entityTable || !draft.selected_tables.includes(entityTable))) {
      setEntityTable(draft.selected_tables[0]);
    }
  }, [draft.selected_tables, entityTable]);

  const refreshEntities = useCallback(async () => {
    if (!draft.id) return;
    try {
      setEntityRecords(await listKnowledgeDatabaseSourceVannaEntities(draft.id));
    } catch (error) {
      setTrainingStatus({ type: "error", message: errorMessage(error) });
    }
  }, [draft.id]);

  useEffect(() => {
    void refreshEntities();
  }, [refreshEntities]);

  const loadEntityCandidates = useCallback(async () => {
    if (!draft.id || !entityTable) {
      setTrainingStatus({ type: "error", message: "请先选择数据库表。" });
      return;
    }
    setTrainingBusy(true);
    setTrainingStatus(null);
    try {
      const candidates = await listKnowledgeDatabaseSourceVannaEntityCandidates(draft.id, {
        table_name: entityTable,
        max_candidates: 12,
      });
      setEntityCandidates(candidates);
      if (candidates[0]) {
        setEntityColumn(candidates[0].column);
        setEntityType(candidates[0].suggested_entity_type);
      }
      setTrainingStatus({ type: "success", message: `识别到 ${candidates.length} 个实体候选字段。` });
    } catch (error) {
      setTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [draft.id, entityTable]);

  const importEntities = useCallback(async () => {
    if (!draft.id) return;
    setTrainingBusy(true);
    setTrainingStatus(null);
    try {
      const result = await importKnowledgeDatabaseSourceVannaEntities(draft.id, {
        table_name: entityTable,
        column: entityColumn,
        entity_type: entityType,
        alias_columns: entityAliasColumns
          .split(/[,，\s]+/)
          .map((item) => item.trim())
          .filter(Boolean),
        max_values: entityMaxValues,
      });
      setTrainingStatus({ type: "success", message: `已导入 ${result.count} 个实体到 ${result.table_column}。` });
      await refreshEntities();
    } catch (error) {
      setTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [draft.id, entityAliasColumns, entityColumn, entityMaxValues, entityTable, entityType, refreshEntities]);

  const deleteEntityRecord = useCallback(
    async (record: VannaEntityRecord) => {
      if (!draft.id) return;
      const entityId = String(record.pk ?? record.id ?? "");
      if (!entityId) return;
      setTrainingBusy(true);
      setTrainingStatus(null);
      try {
        await deleteKnowledgeDatabaseSourceVannaEntity(draft.id, entityId);
        setTrainingStatus({ type: "success", message: "实体已删除。" });
        await refreshEntities();
      } catch (error) {
        setTrainingStatus({ type: "error", message: errorMessage(error) });
      } finally {
        setTrainingBusy(false);
      }
    },
    [draft.id, refreshEntities]
  );

  const trainDdl = useCallback(async () => {
    if (!draft.id) return;
    if (!entityTable) {
      setTrainingStatus({ type: "error", message: "请先选择当前表。" });
      return;
    }
    setTrainingBusy(true);
    setTrainingStatus(null);
    try {
      const result = await trainKnowledgeDatabaseSourceVanna(draft.id, {
        training_type: "ddl",
        table_name: entityTable,
        table_names: [entityTable],
      });
      setTrainingStatus({ type: "success", message: result.message || `${entityTable} 的表结构已同步到 Vanna。` });
      await refreshTrainingData();
    } catch (error) {
      setTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [draft.id, entityTable, refreshTrainingData]);

  const trainDocumentation = useCallback(async () => {
    if (!draft.id) return;
    if (!entityTable) {
      setTrainingStatus({ type: "error", message: "请先选择当前表。" });
      return;
    }
    setTrainingBusy(true);
    setTrainingStatus(null);
    try {
      const result = await trainKnowledgeDatabaseSourceVanna(draft.id, {
        training_type: "documentation",
        table_name: entityTable,
        documentation,
      });
      setDocumentation("");
      setTrainingStatus({ type: "success", message: result.message || `${entityTable} 的业务说明已写入 Vanna。` });
      await refreshTrainingData();
    } catch (error) {
      setTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [documentation, draft.id, entityTable, refreshTrainingData]);

  const trainSqlExample = useCallback(async () => {
    if (!draft.id) return;
    if (!entityTable) {
      setTrainingStatus({ type: "error", message: "请先选择当前表。" });
      return;
    }
    setTrainingBusy(true);
    setTrainingStatus(null);
    try {
      const result = await trainKnowledgeDatabaseSourceVanna(draft.id, {
        training_type: "sql",
        table_name: entityTable,
        question: sqlQuestion,
        sql: sqlExample,
      });
      setSqlQuestion("");
      setSqlExample("");
      setTrainingStatus({ type: "success", message: result.message || `${entityTable} 的 SQL 示例已写入 Vanna。` });
      await refreshTrainingData();
    } catch (error) {
      setTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [draft.id, entityTable, refreshTrainingData, sqlExample, sqlQuestion]);

  const deleteTrainingRecord = useCallback(
    async (record: VannaTrainingRecord) => {
      if (!draft.id) return;
      setTrainingBusy(true);
      setTrainingStatus(null);
      try {
        await deleteKnowledgeDatabaseSourceVannaTraining(draft.id, record.id);
        setTrainingStatus({ type: "success", message: "训练资料已删除。" });
        await refreshTrainingData();
      } catch (error) {
        setTrainingStatus({ type: "error", message: errorMessage(error) });
      } finally {
        setTrainingBusy(false);
      }
    },
    [draft.id, refreshTrainingData]
  );

  const trainingCounts = trainingData?.counts ?? {};
  const recentTrainingRecords = trainingData?.records.slice(0, 8) ?? [];
  const entityTablePrefix = entityTable.includes(".") ? `${entityTable}.` : `public.${entityTable}.`;
  const scopedEntityRecords = entityRecords.filter((record) => !entityTable || String(record.table_column || "").startsWith(entityTablePrefix));

  return (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-950">编辑数据库源</h3>
            <p className="mt-1 text-sm text-gray-500">保存连接信息和可用表，后续数据模型和问数 Agent 会从这里选择数据。</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900"
            aria-label="关闭"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          <div className="grid gap-4 md:grid-cols-2">
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-gray-500">类型</span>
              <input
                value="PostgreSQL"
                readOnly
                className="h-11 w-full rounded-2xl border border-black/[0.08] bg-gray-50 px-4 text-sm text-gray-500 outline-none"
              />
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-gray-500">显示名称</span>
              <input
                value={draft.name}
                onChange={(event) => onUpdate({ name: event.target.value })}
                placeholder="例如：项目 PostgreSQL"
                className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className="text-xs font-semibold text-gray-500">描述</span>
              <input
                value={draft.description || ""}
                onChange={(event) => onUpdate({ description: event.target.value })}
                placeholder="这组表主要用来分析什么"
                className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              />
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-gray-500">Host</span>
              <input
                value={draft.host}
                disabled={isProjectDefault}
                onChange={(event) => onUpdate({ host: event.target.value })}
                className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              />
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-gray-500">端口</span>
              <input
                type="number"
                value={draft.port}
                disabled={isProjectDefault}
                onChange={(event) => onUpdate({ port: Number(event.target.value) || 5432 })}
                className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              />
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-gray-500">数据库名</span>
              <input
                value={draft.database}
                disabled={isProjectDefault}
                onChange={(event) => onUpdate({ database: event.target.value })}
                className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              />
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-gray-500">用户名</span>
              <input
                value={draft.username}
                disabled={isProjectDefault}
                onChange={(event) => onUpdate({ username: event.target.value })}
                className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              />
            </label>
            <label className="space-y-1.5 md:col-span-2">
              <span className="text-xs font-semibold text-gray-500">密码</span>
              <input
                type="password"
                value={draft.password || ""}
                disabled={isProjectDefault}
                onChange={(event) => onUpdate({ password: event.target.value })}
                placeholder={draft.password_configured ? "已配置，留空不修改" : "请输入密码"}
                className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              />
            </label>
          </div>

          {status ? (
            <div
              className={`mt-5 flex items-start gap-2 rounded-2xl border px-4 py-3 text-sm ${
                status.type === "success"
                  ? "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                  : status.type === "error"
                    ? "border-red-500/15 bg-red-50 text-red-600"
                    : "border-[#002fa7]/15 bg-[#002fa7]/[0.05] text-[#002fa7]"
              }`}
            >
              {status.type === "success" ? (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              ) : status.type === "error" ? (
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              ) : (
                <Database className="mt-0.5 h-4 w-4 shrink-0" />
              )}
              <span className="break-words">{status.message}</span>
            </div>
          ) : null}

          <div className="mt-5 rounded-3xl border border-black/[0.06] bg-black/[0.018] p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-gray-950">选择表</p>
                <p className="mt-1 text-xs text-gray-400">只保存你希望问数 Agent 使用的表。</p>
              </div>
              <button
                type="button"
                onClick={onLoadTables}
                disabled={busy || !draft.id}
                className="inline-flex h-9 items-center gap-2 rounded-full bg-white px-3 text-xs font-semibold text-[#002fa7] shadow-sm ring-1 ring-black/[0.05] transition hover:bg-[#002fa7]/[0.04] disabled:cursor-not-allowed disabled:opacity-45"
              >
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                读取表
              </button>
            </div>
            <div className="mt-3 max-h-52 overflow-y-auto rounded-2xl bg-white p-2">
              {tables.length > 0 ? (
                <div className="grid gap-1.5 sm:grid-cols-2">
                  {tables.map((table) => {
                    const checked = draft.selected_tables.includes(table);
                    return (
                      <label
                        key={table}
                        className={`flex cursor-pointer items-center gap-2 rounded-xl px-2.5 py-2 text-xs transition ${
                          checked ? "bg-[#002fa7]/10 text-[#002fa7]" : "text-gray-600 hover:bg-black/[0.035]"
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(event) => {
                            const next = event.target.checked
                              ? [...draft.selected_tables, table]
                              : draft.selected_tables.filter((item) => item !== table);
                            onUpdate({ selected_tables: next });
                          }}
                          className="h-3.5 w-3.5 accent-[#002fa7]"
                        />
                        <span className="min-w-0 truncate" title={table}>{table}</span>
                      </label>
                    );
                  })}
                </div>
              ) : (
                <p className="px-3 py-6 text-center text-xs text-gray-400">点击“读取表”后，在这里勾选可用于问数的表。</p>
              )}
            </div>
          </div>

          <div className="mt-5 rounded-3xl border border-[#002fa7]/10 bg-[#002fa7]/[0.025] p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-gray-950">表级 Vanna 训练</p>
                <p className="mt-1 text-xs leading-5 text-gray-500">
                  连接只是归属；实际训练、实体字典和 SQL 示例都围绕当前数据库表维护。
                </p>
              </div>
              <button
                type="button"
                onClick={refreshTrainingData}
                disabled={trainingLoading || !draft.id}
                className="inline-flex h-9 items-center gap-2 rounded-full bg-white px-3 text-xs font-semibold text-[#002fa7] shadow-sm ring-1 ring-black/[0.05] transition hover:bg-[#002fa7]/[0.04] disabled:cursor-wait disabled:opacity-45"
              >
                {trainingLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                刷新
              </button>
            </div>

            <div className="mt-4 rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-gray-400">当前表</p>
                  <p className="mt-1 text-sm font-semibold text-gray-950">{entityTable || "未选择表"}</p>
                </div>
                <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-semibold text-[#002fa7]">
                  {draft.selected_tables.length} 张已选表
                </span>
              </div>
              <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
                {draft.selected_tables.length > 0 ? (
                  draft.selected_tables.map((table) => (
                    <button
                      key={table}
                      type="button"
                      onClick={() => {
                        setEntityTable(table);
                        setEntityCandidates([]);
                        setEntityColumn("");
                        setEntityType("");
                        setTrainingStatus(null);
                      }}
                      className={`shrink-0 rounded-2xl px-3 py-2 text-xs font-semibold transition ${
                        entityTable === table
                          ? "bg-[#002fa7] text-white shadow-sm"
                          : "bg-gray-50 text-gray-600 hover:bg-[#002fa7]/[0.06] hover:text-[#002fa7]"
                      }`}
                      title={table}
                    >
                      {table}
                    </button>
                  ))
                ) : (
                  <p className="rounded-2xl bg-gray-50 px-3 py-3 text-xs text-gray-400">先在上方读取并勾选数据库表。</p>
                )}
              </div>
            </div>

            {trainingStatus ? (
              <div
                className={`mt-3 flex items-start gap-2 rounded-2xl border px-3 py-2 text-xs ${
                  trainingStatus.type === "success"
                    ? "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                    : "border-red-500/15 bg-red-50 text-red-600"
                }`}
              >
                {trainingStatus.type === "success" ? (
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                ) : (
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                )}
                <span>{trainingStatus.message}</span>
              </div>
            ) : null}

            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">DDL</p>
                <p className="mt-1 text-lg font-semibold text-gray-950">{trainingCounts.ddl ?? 0}</p>
              </div>
              <div className="rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">SQL 示例</p>
                <p className="mt-1 text-lg font-semibold text-gray-950">{trainingCounts.sql ?? 0}</p>
              </div>
              <div className="rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">业务说明</p>
                <p className="mt-1 text-lg font-semibold text-gray-950">{trainingCounts.documentation ?? 0}</p>
              </div>
            </div>

            <div className="mt-4 rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-gray-950">同步当前表结构</p>
                  <p className="mt-1 text-xs text-gray-400">只把当前表的字段结构写入 Vanna。</p>
                </div>
                <button
                  type="button"
                  onClick={trainDdl}
                  disabled={trainingBusy || !draft.id || !entityTable}
                  className="inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Database className="h-3.5 w-3.5" />}
                  同步当前表 DDL
                </button>
              </div>
            </div>

            <div className="mt-4 rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-gray-950">实体字典</p>
                  <p className="mt-1 text-xs text-gray-400">为当前表导入标准值和别名，帮助 Vanna 把业务词映射到正确字段。</p>
                </div>
                <button
                  type="button"
                  onClick={loadEntityCandidates}
                  disabled={trainingBusy || !draft.id || !entityTable}
                  className="inline-flex h-9 items-center gap-2 rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.06] px-3 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/10 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                  识别候选
                </button>
              </div>

              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <label className="space-y-1.5">
                  <span className="text-xs font-semibold text-gray-500">当前表</span>
                  <input
                    value={entityTable}
                    readOnly
                    className="h-10 w-full rounded-2xl border border-black/[0.08] bg-gray-50 px-3 text-xs text-gray-500 outline-none"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-xs font-semibold text-gray-500">实体字段</span>
                  <input
                    value={entityColumn}
                    onChange={(event) => setEntityColumn(event.target.value)}
                    placeholder="例如：series_name"
                    className="h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-xs font-semibold text-gray-500">实体类型</span>
                  <input
                    value={entityType}
                    onChange={(event) => setEntityType(event.target.value)}
                    placeholder="例如：vehicle_series / product / region"
                    className="h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-xs font-semibold text-gray-500">别名字段（可选）</span>
                  <input
                    value={entityAliasColumns}
                    onChange={(event) => setEntityAliasColumns(event.target.value)}
                    placeholder="多个字段用逗号分隔"
                    className="h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                  />
                </label>
              </div>

              {entityCandidates.length > 0 ? (
                <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
                  {entityCandidates.map((candidate) => (
                    <button
                      key={candidate.column}
                      type="button"
                      onClick={() => {
                        setEntityColumn(candidate.column);
                        setEntityType(candidate.suggested_entity_type);
                      }}
                      className={`shrink-0 rounded-2xl px-3 py-2 text-left text-xs transition ${
                        entityColumn === candidate.column
                          ? "bg-[#002fa7] text-white"
                          : "bg-gray-50 text-gray-600 hover:bg-[#002fa7]/[0.06] hover:text-[#002fa7]"
                      }`}
                    >
                      <span className="block font-semibold">{candidate.column}</span>
                      <span className="mt-0.5 block opacity-70">推荐度 {Math.round((candidate.score || 0) * 100)}%</span>
                    </button>
                  ))}
                </div>
              ) : null}

              <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                <label className="flex items-center gap-2 text-xs text-gray-500">
                  最多导入
                  <input
                    type="number"
                    min={1}
                    max={10000}
                    value={entityMaxValues}
                    onChange={(event) => setEntityMaxValues(Number(event.target.value) || 1000)}
                    className="h-9 w-24 rounded-2xl border border-black/[0.08] px-3 text-xs outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                  />
                  个值
                </label>
                <button
                  type="button"
                  onClick={importEntities}
                  disabled={trainingBusy || !draft.id || !entityTable || !entityColumn.trim() || !entityType.trim()}
                  className="inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                  导入实体
                </button>
              </div>

              <div className="mt-4">
                <div className="flex items-center justify-between">
                  <p className="text-xs font-semibold text-gray-500">当前表实体</p>
                  <span className="text-xs text-gray-400">{scopedEntityRecords.length} 个</span>
                </div>
                <div className="mt-2 max-h-52 space-y-2 overflow-y-auto">
                  {scopedEntityRecords.length > 0 ? (
                    scopedEntityRecords.slice(0, 30).map((record) => (
                      <div key={String(record.pk ?? record.id ?? `${record.table_column}-${record.canonical_name}`)} className="flex items-start justify-between gap-3 rounded-2xl bg-gray-50 px-3 py-2">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="rounded-full bg-[#002fa7]/10 px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">{record.entity_type}</span>
                            <span className="truncate text-xs font-semibold text-gray-800">{record.canonical_name}</span>
                          </div>
                          <p className="mt-1 line-clamp-1 text-[11px] text-gray-400">
                            {record.table_column}{record.aliases?.length ? ` · 别名：${record.aliases.slice(0, 5).join("、")}` : ""}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => deleteEntityRecord(record)}
                          disabled={trainingBusy}
                          className="shrink-0 rounded-full px-2 py-1 text-[11px] font-semibold text-gray-400 transition hover:bg-red-50 hover:text-red-600 disabled:cursor-wait disabled:opacity-40"
                        >
                          删除
                        </button>
                      </div>
                    ))
                  ) : (
                    <p className="rounded-2xl bg-gray-50 px-3 py-4 text-center text-xs text-gray-400">当前表还没有导入实体。</p>
                  )}
                </div>
              </div>
            </div>

            <div className="mt-3 grid gap-3 lg:grid-cols-2">
              <div className="rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
                <p className="text-sm font-semibold text-gray-950">SQL 示例</p>
                <input
                  value={sqlQuestion}
                  onChange={(event) => setSqlQuestion(event.target.value)}
                  placeholder="例：按当前表统计配置率"
                  className="mt-3 h-10 w-full rounded-2xl border border-black/[0.08] px-3 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
                <textarea
                  value={sqlExample}
                  onChange={(event) => setSqlExample(event.target.value)}
                  placeholder="SELECT ..."
                  rows={4}
                  className="mt-2 w-full resize-none rounded-2xl border border-black/[0.08] px-3 py-2 font-mono text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
                <button
                  type="button"
                  onClick={trainSqlExample}
                  disabled={trainingBusy || !draft.id || !sqlQuestion.trim() || !sqlExample.trim()}
                  className="mt-2 inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                  保存示例
                </button>
              </div>

              <div className="rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
                <p className="text-sm font-semibold text-gray-950">业务说明</p>
                <textarea
                  value={documentation}
                  onChange={(event) => setDocumentation(event.target.value)}
                  placeholder="写当前表的字段含义、业务口径、常见过滤条件等。"
                  rows={6}
                  className="mt-3 w-full resize-none rounded-2xl border border-black/[0.08] px-3 py-2 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
                <button
                  type="button"
                  onClick={trainDocumentation}
                  disabled={trainingBusy || !draft.id || !documentation.trim()}
                  className="mt-2 inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BookOpenText className="h-3.5 w-3.5" />}
                  保存说明
                </button>
              </div>
            </div>

            <div className="mt-4 rounded-2xl bg-white p-3 shadow-sm ring-1 ring-black/[0.04]">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-semibold text-gray-950">当前表训练资料</p>
                <span className="text-xs text-gray-400">{trainingData?.count ?? 0} 条</span>
              </div>
              <div className="mt-2 space-y-2">
                {recentTrainingRecords.length > 0 ? (
                  recentTrainingRecords.map((record) => (
                    <div key={record.id} className="flex items-start justify-between gap-3 rounded-2xl bg-gray-50 px-3 py-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="rounded-full bg-[#002fa7]/10 px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">
                            {record.training_type === "documentation" ? "文档" : record.training_type.toUpperCase()}
                          </span>
                          {record.question ? <span className="truncate text-xs font-semibold text-gray-800">{record.question}</span> : null}
                        </div>
                        <p className="mt-1 line-clamp-2 break-all text-xs leading-5 text-gray-500">{record.preview || record.content}</p>
                      </div>
                      <button
                        type="button"
                        onClick={() => deleteTrainingRecord(record)}
                        disabled={trainingBusy}
                        className="shrink-0 rounded-full px-2 py-1 text-[11px] font-semibold text-gray-400 transition hover:bg-red-50 hover:text-red-600 disabled:cursor-wait disabled:opacity-40"
                      >
                        删除
                      </button>
                    </div>
                  ))
                ) : (
                  <p className="rounded-2xl bg-gray-50 px-3 py-5 text-center text-xs text-gray-400">
                    当前表还没有 Vanna 训练资料。先同步 DDL，或者添加 SQL 示例。
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-3 border-t border-black/[0.06] px-6 py-4">
          <button
            type="button"
            onClick={onTest}
            disabled={busy}
            className="inline-flex h-10 items-center gap-2 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700 transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
            测试连接
          </button>
          <button
            type="button"
            onClick={onClose}
            className="h-10 rounded-2xl px-4 text-sm font-semibold text-gray-500 transition hover:bg-black/[0.04]"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onSave}
            disabled={busy || !draft.name.trim()}
            className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            保存
          </button>
        </div>
      </div>
    </div>
  );
}

function TableAssetCard({
  asset,
  profileLoadingId,
  profilingAssetId,
  onOpenProfile,
  onGenerateProfile,
}: {
  asset: TableAsset;
  profileLoadingId: string | null;
  profilingAssetId: string | null;
  onOpenProfile: (asset: TableAsset) => void;
  onGenerateProfile: (asset: TableAsset) => void;
}) {
  return (
    <article className="rounded-3xl border border-black/[0.06] bg-white px-4 py-4 shadow-sm transition hover:border-[#002fa7]/20 hover:bg-[#002fa7]/[0.015]">
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
        <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
          {asset.profile_status === "ready" ? (
            <button
              type="button"
              onClick={() => onOpenProfile(asset)}
              disabled={profileLoadingId === asset.asset_id}
              className="rounded-2xl bg-[#002fa7]/10 px-3.5 py-2 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/15 disabled:cursor-wait disabled:opacity-60"
            >
              {profileLoadingId === asset.asset_id ? "打开中" : "查看 Profile"}
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => onGenerateProfile(asset)}
            disabled={profilingAssetId === asset.asset_id}
            className="rounded-2xl border border-black/[0.08] bg-white px-3.5 py-2 text-xs font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
          >
            {profilingAssetId === asset.asset_id ? "生成中" : asset.profile_status === "ready" ? "重新生成" : "生成"}
          </button>
        </div>
      </div>
    </article>
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
