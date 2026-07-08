"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  AlertCircle,
  BookOpenText,
  Bot,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
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
  databaseQueryResultExportCsvUrl,
  generateTableAssetProfile,
  getDatabaseQueryResultPage,
  getTableAsset,
  deleteKnowledgeDatabaseSourceVannaEntity,
  deleteKnowledgeDatabaseSourceVannaTraining,
  importKnowledgeDatabaseSourceVannaEntities,
  listTableAssetEntityCandidates,
  listKnowledgeDatabaseSourceVannaEntities,
  listKnowledgeDatabaseSourceVannaEntityCandidates,
  listKnowledgeDatabaseSourceVannaTraining,
  listDatabaseQueryResults,
  listKnowledgeDatabaseSourceTables,
  listKnowledgeDatabaseSources,
  listTableAssets,
  refreshTableAssetProfiles,
  saveKnowledgeDatabaseSource,
  testKnowledgeDatabaseSource,
  trainKnowledgeDatabaseSourceVanna,
  type DatabaseQueryResultPage,
  type DatabaseQueryResultSummary,
  type KnowledgeDatabaseSource,
  type TableAsset,
  type TableEntityCandidate,
  type VannaEntityListResult,
  type VannaEntityRecord,
  type VannaTrainingData,
  type VannaTrainingRecord,
} from "@/lib/api";
import { getSettings, updateSettings } from "@/lib/settingsApi";
import { useApp } from "@/lib/store";

type AnalyticsSection = "results" | "assets" | "models" | "measures" | "agent";
type TrainingStatus = { type: "success" | "error"; message: string; jobId?: string | null };
type ActionDialog = { type: "success" | "error"; title: string; message: string };
const ENTITY_PAGE_SIZE = 10;
type QueuedEntityImport = {
  jobId: string;
  tableName: string;
  column: string;
  entityType: string;
  supportColumnsKey: string;
};

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

function vannaTrainingKind(record: VannaTrainingRecord): "sql" | "ddl" | "documentation" | "unknown" {
  const type = String(record.training_type || "").toLowerCase();
  if (type === "sql" || type === "ddl" || type === "documentation") return type;
  if (record.question) return "sql";
  const content = String(record.content || record.preview || "").trim().toLowerCase();
  if (content.startsWith("create table")) return "ddl";
  if (content) return "documentation";
  return "unknown";
}

function positiveIntOrNull(value: string): number | null {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatCellValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
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
  const [queryResults, setQueryResults] = useState<DatabaseQueryResultSummary[]>([]);
  const [selectedQueryResultId, setSelectedQueryResultId] = useState("");
  const [queryResultPage, setQueryResultPage] = useState<DatabaseQueryResultPage | null>(null);
  const [queryResultsLoading, setQueryResultsLoading] = useState(false);
  const [queryResultPageLoading, setQueryResultPageLoading] = useState(false);
  const [queryResultPageNumber, setQueryResultPageNumber] = useState(1);
  const [queryResultPageSize, setQueryResultPageSize] = useState("100");

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

  const loadQueryResults = useCallback(async () => {
    setQueryResultsLoading(true);
    try {
      const results = await listDatabaseQueryResults(50);
      setQueryResults(results);
      setSelectedQueryResultId((current) => current || results[0]?.result_id || "");
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setQueryResultsLoading(false);
    }
  }, []);

  const loadQueryResultPage = useCallback(async () => {
    if (!selectedQueryResultId) {
      setQueryResultPage(null);
      return;
    }
    setQueryResultPageLoading(true);
    try {
      const page = await getDatabaseQueryResultPage(
        selectedQueryResultId,
        queryResultPageNumber,
        positiveIntOrNull(queryResultPageSize) ?? 100
      );
      setQueryResultPage(page);
    } catch (error) {
      setQueryResultPage(null);
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setQueryResultPageLoading(false);
    }
  }, [queryResultPageNumber, queryResultPageSize, selectedQueryResultId]);

  useEffect(() => {
    if (activeSection === "results") {
      void loadQueryResults();
    }
  }, [activeSection, loadQueryResults]);

  useEffect(() => {
    if (activeSection === "results" && selectedQueryResultId) {
      void loadQueryResultPage();
    }
  }, [activeSection, loadQueryResultPage, selectedQueryResultId]);

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
                      active={activeSection === "measures"}
                      icon={Sigma}
                      title="度量值"
                      description="指标口径"
                      onClick={() => setActiveSection("measures")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "models"}
                      icon={Layers3}
                      title="数据模型"
                      description="模型 reference"
                      onClick={() => setActiveSection("models")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "agent"}
                      icon={Bot}
                      title="问数 Agent"
                      description="专门对话"
                      onClick={() => setActiveSection("agent")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "results"}
                      icon={Database}
                      title="查询结果"
                      description={queryResultsLoading ? "读取中" : "分页明细"}
                      onClick={() => setActiveSection("results")}
                    />
                  </nav>
                </aside>

                <div className="min-w-0 rounded-[28px] border border-black/[0.06] bg-white shadow-sm">
                  {activeSection === "results" ? (
                    <DatabaseQueryResultsSection
                      queryResults={queryResults}
                      queryResultPage={queryResultPage}
                      selectedQueryResultId={selectedQueryResultId}
                      queryResultsLoading={queryResultsLoading}
                      queryResultPageLoading={queryResultPageLoading}
                      queryResultPageNumber={queryResultPageNumber}
                      queryResultPageSize={queryResultPageSize}
                      onRefreshResults={loadQueryResults}
                      onRefreshPage={loadQueryResultPage}
                      onSelectResult={(resultId) => {
                        setSelectedQueryResultId(resultId);
                        setQueryResultPageNumber(1);
                      }}
                      onChangeResultId={(resultId) => {
                        setSelectedQueryResultId(resultId);
                        setQueryResultPageNumber(1);
                      }}
                      onChangePageSize={(pageSize) => {
                        setQueryResultPageSize(pageSize);
                        setQueryResultPageNumber(1);
                      }}
                      onPreviousPage={() => setQueryResultPageNumber((value) => Math.max(1, value - 1))}
                      onNextPage={() => setQueryResultPageNumber((value) => value + 1)}
                    />
                  ) : null}

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
                                  <DatabaseSourceCard
                                    key={source.id}
                                    source={source}
                                    onManage={openDatabaseSourceModal}
                                    onTrainTable={openVannaTrainingModal}
                                  />
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

      {vannaTrainingTarget ? (
        <TableVannaTrainingModal
          source={vannaTrainingTarget.source}
          tableName={vannaTrainingTarget.table}
          availableTables={vannaTrainingTarget.source.selected_tables ?? []}
          onTableChange={(table) =>
            setVannaTrainingTarget((current) => (current ? { ...current, table } : current))
          }
          onClose={() => setVannaTrainingTarget(null)}
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

function DatabaseQueryResultsSection({
  queryResults,
  queryResultPage,
  selectedQueryResultId,
  queryResultsLoading,
  queryResultPageLoading,
  queryResultPageNumber,
  queryResultPageSize,
  onRefreshResults,
  onRefreshPage,
  onSelectResult,
  onChangeResultId,
  onChangePageSize,
  onPreviousPage,
  onNextPage,
}: {
  queryResults: DatabaseQueryResultSummary[];
  queryResultPage: DatabaseQueryResultPage | null;
  selectedQueryResultId: string;
  queryResultsLoading: boolean;
  queryResultPageLoading: boolean;
  queryResultPageNumber: number;
  queryResultPageSize: string;
  onRefreshResults: () => void;
  onRefreshPage: () => void;
  onSelectResult: (resultId: string) => void;
  onChangeResultId: (resultId: string) => void;
  onChangePageSize: (pageSize: string) => void;
  onPreviousPage: () => void;
  onNextPage: () => void;
}) {
  const selectedSummary = queryResults.find((item) => item.result_id === selectedQueryResultId);
  const exportEnabled = queryResultPage?.export_enabled ?? selectedSummary?.export_enabled ?? false;

  return (
    <section className="p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-black/[0.06] pb-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[#002fa7]/[0.06] text-[#002fa7]">
            <Database className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-gray-950">查询结果</h2>
            <p className="mt-0.5 text-xs text-gray-500">持久化结果、分页明细和 CSV 导出。</p>
          </div>
        </div>
        <button
          type="button"
          onClick={onRefreshResults}
          disabled={queryResultsLoading}
          className="inline-flex h-8 items-center gap-1.5 rounded-xl border border-black/[0.08] bg-white px-3 text-xs font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${queryResultsLoading ? "animate-spin" : ""}`} />
          刷新
        </button>
      </div>

      <div className="mt-3 grid gap-3 xl:grid-cols-[300px_minmax(0,1fr)]">
        <div className="min-h-[420px] rounded-2xl border border-black/[0.06] bg-white shadow-sm">
          <div className="flex items-center justify-between gap-2 border-b border-black/[0.06] px-3 py-2">
            <span className="text-xs font-semibold text-gray-800">最近结果</span>
            <span className="text-xs text-gray-400">{queryResultsLoading ? "读取中" : `最近 ${queryResults.length} 个`}</span>
          </div>
          <div className="max-h-[620px] overflow-auto p-2">
            {queryResults.length === 0 && !queryResultsLoading ? (
              <div className="rounded-xl border border-dashed border-black/[0.08] px-3 py-8 text-center text-xs text-gray-400">
                暂无持久化查询结果
              </div>
            ) : (
              queryResults.map((item) => {
                const active = item.result_id === selectedQueryResultId;
                return (
                  <button
                    key={item.result_id}
                    type="button"
                    onClick={() => onSelectResult(item.result_id)}
                    className={`mb-1.5 w-full rounded-xl border px-2.5 py-2 text-left transition ${
                      active ? "border-[#002fa7]/30 bg-[#002fa7]/[0.06]" : "border-black/[0.05] bg-white hover:bg-gray-50"
                    }`}
                  >
                    <div className="flex min-w-0 items-center justify-between gap-2">
                      <span className="truncate font-mono text-xs font-semibold text-gray-800">{item.result_id}</span>
                      <span
                        className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                          item.expired ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700"
                        }`}
                      >
                        {item.expired ? "已过期" : "可用"}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-1 text-[11px] leading-4 text-gray-500">{item.question || item.sql}</p>
                    <p className="mt-1 text-[10px] text-gray-400">{item.row_count} 行 · {formatDateTime(item.created_at)}</p>
                  </button>
                );
              })
            )}
          </div>
        </div>

        <div className="min-w-0 rounded-2xl border border-black/[0.06] bg-white shadow-sm">
          {!selectedQueryResultId ? (
            <div className="flex min-h-[420px] items-center justify-center text-sm text-gray-400">选择一个查询结果查看明细</div>
          ) : (
            <div className="min-w-0">
              <div className="border-b border-black/[0.06] p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-mono text-xs font-semibold text-gray-900">{selectedQueryResultId}</p>
                    <p className="mt-0.5 truncate text-[11px] text-gray-400">
                      {queryResultPage?.row_count ?? "-"} 行 · 过期时间 {queryResultPage?.expires_at ? formatDateTime(queryResultPage.expires_at) : "-"}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {exportEnabled ? (
                      <a
                        href={databaseQueryResultExportCsvUrl(selectedQueryResultId)}
                        className="inline-flex h-8 items-center gap-1.5 rounded-xl bg-emerald-50 px-2.5 text-xs font-semibold text-emerald-700 transition hover:bg-emerald-100"
                      >
                        <Download className="h-3.5 w-3.5" />
                        导出 CSV
                      </a>
                    ) : (
                      <span className="inline-flex h-8 items-center gap-1.5 rounded-xl bg-gray-100 px-2.5 text-xs font-semibold text-gray-400">
                        <Download className="h-3.5 w-3.5" />
                        导出已关闭
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={onRefreshPage}
                      disabled={queryResultPageLoading}
                      className="inline-flex h-8 items-center gap-1.5 rounded-xl bg-[#002fa7]/10 px-2.5 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/15 disabled:opacity-50"
                    >
                      <RefreshCw className={`h-3.5 w-3.5 ${queryResultPageLoading ? "animate-spin" : ""}`} />
                      刷新页
                    </button>
                  </div>
                </div>
                <div className="mt-2 grid gap-2 md:grid-cols-[minmax(0,1fr)_90px_126px]">
                  <input
                    value={selectedQueryResultId}
                    onChange={(event) => onChangeResultId(event.target.value.trim())}
                    className="h-8 rounded-xl border border-black/[0.08] bg-white px-2.5 font-mono text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    placeholder="result_id"
                  />
                  <input
                    value={queryResultPageSize}
                    onChange={(event) => onChangePageSize(event.target.value)}
                    className="h-8 rounded-xl border border-black/[0.08] bg-white px-2.5 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    inputMode="numeric"
                    placeholder="page_size"
                  />
                  <div className="flex items-center justify-end gap-1.5">
                    <button
                      type="button"
                      disabled={!queryResultPage?.has_previous || queryResultPageLoading}
                      onClick={onPreviousPage}
                      title="上一页"
                      className="inline-flex h-8 w-8 items-center justify-center rounded-xl border border-black/[0.08] bg-white text-gray-600 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-35"
                    >
                      <ChevronLeft className="h-4 w-4" />
                    </button>
                    <span className="min-w-12 text-center text-xs font-semibold text-gray-500">第 {queryResultPageNumber} 页</span>
                    <button
                      type="button"
                      disabled={!queryResultPage?.has_next || queryResultPageLoading}
                      onClick={onNextPage}
                      title="下一页"
                      className="inline-flex h-8 w-8 items-center justify-center rounded-xl border border-black/[0.08] bg-white text-gray-600 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-35"
                    >
                      <ChevronRight className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              </div>

              {queryResultPage?.expired ? (
                <div className="p-8 text-center text-sm text-amber-600">{queryResultPage.message || "结果已过期，请重新执行问数。"}</div>
              ) : (
                <div className="max-h-[680px] overflow-auto">
                  <table className="w-full min-w-max border-collapse text-left text-[11px]">
                    <thead className="sticky top-0 bg-slate-50 text-[11px] text-gray-500">
                      <tr>
                        {(queryResultPage?.columns || []).map((column) => (
                          <th key={column} className="border-b border-black/[0.06] px-2.5 py-1.5 font-semibold">
                            {column}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {(queryResultPage?.rows || []).map((row, index) => (
                        <tr key={index} className="border-b border-black/[0.04] odd:bg-white even:bg-slate-50/40">
                          {(queryResultPage?.columns || []).map((column) => (
                            <td key={column} className="max-w-[260px] truncate px-2.5 py-1.5 text-gray-700" title={String(row[column] ?? "")}>
                              {formatCellValue(row[column])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {queryResultPage && queryResultPage.rows.length === 0 ? (
                    <div className="p-8 text-center text-sm text-gray-400">当前页没有数据</div>
                  ) : null}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function PlusIcon() {
  return <span className="text-lg leading-none">+</span>;
}

function DatabaseSourceCard({
  source,
  onManage,
  onTrainTable,
}: {
  source: KnowledgeDatabaseSource;
  onManage: (source: KnowledgeDatabaseSource) => void;
  onTrainTable: (source: KnowledgeDatabaseSource, table: string) => void;
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
      {tableCount > 0 ? (
        <div className="mt-4 rounded-2xl bg-gray-50 p-2">
          <div className="mb-2 flex items-center justify-between px-1">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">已选数据库表</p>
            <span className="text-[11px] text-gray-400">Vanna 按表训练</span>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {(source.selected_tables ?? []).map((table) => (
              <button
                key={table}
                type="button"
                onClick={() => onTrainTable(source, table)}
                className="flex min-w-0 items-center justify-between gap-3 rounded-xl bg-white px-3 py-2 text-left text-xs shadow-sm ring-1 ring-black/[0.04] transition hover:bg-[#002fa7]/[0.035] hover:ring-[#002fa7]/15"
                title={table}
              >
                <span className="min-w-0 truncate font-medium text-gray-800">{table}</span>
                <span className="shrink-0 rounded-full bg-[#002fa7]/10 px-2 py-0.5 font-semibold text-[#002fa7]">训练</span>
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </article>
  );
}

function TableVannaTrainingModal({
  source,
  tableName,
  availableTables,
  onTableChange,
  onClose,
}: {
  source: KnowledgeDatabaseSource;
  tableName: string;
  availableTables: string[];
  onTableChange: (tableName: string) => void;
  onClose: () => void;
}) {
  const [trainingData, setTrainingData] = useState<VannaTrainingData | null>(null);
  const [trainingLoading, setTrainingLoading] = useState(false);
  const [loadedTrainingKey, setLoadedTrainingKey] = useState("");
  const [trainingBusy, setTrainingBusy] = useState(false);
  const [trainingStatus, setTrainingStatus] = useState<TrainingStatus | null>(null);
  const [documentation, setDocumentation] = useState("");
  const [sqlQuestion, setSqlQuestion] = useState("");
  const [sqlExample, setSqlExample] = useState("");
  const [savedRecordsDialog, setSavedRecordsDialog] = useState<"sql" | "documentation" | null>(null);
  const [entityCandidates, setEntityCandidates] = useState<TableEntityCandidate[]>([]);
  const [entityRecords, setEntityRecords] = useState<VannaEntityRecord[]>([]);
  const [entityListSummary, setEntityListSummary] = useState<Pick<VannaEntityListResult, "count" | "limited" | "type_counts">>({
    count: 0,
    limited: false,
    type_counts: {},
  });
  const [entityColumn, setEntityColumn] = useState("");
  const [entityType, setEntityType] = useState("");
  const [entitySupportColumns, setEntitySupportColumns] = useState<string[]>([]);
  const [queuedEntityImport, setQueuedEntityImport] = useState<QueuedEntityImport | null>(null);
  const [entityListExpanded, setEntityListExpanded] = useState(false);
  const [entitySearch, setEntitySearch] = useState("");
  const [debouncedEntitySearch, setDebouncedEntitySearch] = useState("");
  const [entityTypeFilter, setEntityTypeFilter] = useState("all");
  const [entityPage, setEntityPage] = useState(1);
  const [entityTopKDefault, setEntityTopKDefault] = useState("10");
  const [entityTopKByType, setEntityTopKByType] = useState<Record<string, string>>({});
  const [entityTopKSaving, setEntityTopKSaving] = useState(false);
  const [actionDialog, setActionDialog] = useState<ActionDialog | null>(null);
  const trainingStatusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showTrainingStatus = useCallback((status: TrainingStatus | null) => {
    if (trainingStatusTimerRef.current) {
      clearTimeout(trainingStatusTimerRef.current);
      trainingStatusTimerRef.current = null;
    }
    setTrainingStatus(status);
    if (status?.type === "success" && !status.jobId) {
      trainingStatusTimerRef.current = setTimeout(() => {
        setTrainingStatus(null);
        trainingStatusTimerRef.current = null;
      }, 3000);
    }
  }, []);

  useEffect(() => {
    return () => {
      if (trainingStatusTimerRef.current) {
        clearTimeout(trainingStatusTimerRef.current);
      }
    };
  }, []);

  const loadEntityTopKSettings = useCallback(async () => {
    try {
      const settings = await getSettings();
      setEntityTopKDefault(String(settings.vanna?.query?.entity_top_k_default ?? 10));
      setEntityTopKByType(
        Object.fromEntries(
          Object.entries(settings.vanna?.query?.entity_top_k_by_type ?? {}).map(([key, value]) => [key, String(value)])
        )
      );
    } catch (error) {
      showTrainingStatus({ type: "error", message: errorMessage(error) });
    }
  }, [showTrainingStatus]);

  useEffect(() => {
    void loadEntityTopKSettings();
  }, [loadEntityTopKSettings]);

  const saveEntityTopKSettings = useCallback(async () => {
    const defaultTopK = positiveIntOrNull(entityTopKDefault) ?? 10;
    const byType = Object.entries(entityTopKByType).reduce<Record<string, number>>((acc, [type, value]) => {
      const entityTypeKey = type.trim();
      const topK = positiveIntOrNull(value);
      if (entityTypeKey && topK) acc[entityTypeKey] = topK;
      return acc;
    }, {});
    setEntityTopKSaving(true);
    try {
      await updateSettings({
        vanna: {
          query: {
            entity_top_k_default: defaultTopK,
            entity_top_k_by_type: byType,
          },
        },
      });
      setEntityTopKDefault(String(defaultTopK));
      setEntityTopKByType(Object.fromEntries(Object.entries(byType).map(([key, value]) => [key, String(value)])));
      setActionDialog({
        type: "success",
        title: "配置已保存",
        message: "实体召回配置已保存，下一次数据库问数会使用这组设置。",
      });
    } catch (error) {
      setActionDialog({
        type: "error",
        title: "保存失败",
        message: errorMessage(error),
      });
    } finally {
      setEntityTopKSaving(false);
    }
  }, [entityTopKByType, entityTopKDefault]);

  const refreshTrainingData = useCallback(async () => {
    if (!source.id || !tableName) return;
    setTrainingLoading(true);
    try {
      const data = await listKnowledgeDatabaseSourceVannaTraining(source.id, tableName);
      setTrainingData(data);
      setLoadedTrainingKey(`${source.id}:${tableName}`);
    } catch (error) {
      setLoadedTrainingKey(`${source.id}:${tableName}`);
      showTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingLoading(false);
    }
  }, [showTrainingStatus, source.id, tableName]);

  const refreshTrainingDataSoon = useCallback(() => {
    window.setTimeout(() => {
      void refreshTrainingData();
    }, 800);
  }, [refreshTrainingData]);

  useEffect(() => {
    void refreshTrainingData();
  }, [refreshTrainingData]);

  useEffect(() => {
    setLoadedTrainingKey("");
    setTrainingData(null);
  }, [source.id, tableName]);

  const refreshEntities = useCallback(async () => {
    if (!source.id || !tableName) return;
    try {
      const result = await listKnowledgeDatabaseSourceVannaEntities(source.id, {
        tableName,
        entityType: entityTypeFilter === "all" ? undefined : entityTypeFilter,
        search: debouncedEntitySearch,
        offset: (Math.max(1, entityPage) - 1) * ENTITY_PAGE_SIZE,
        limit: ENTITY_PAGE_SIZE,
      });
      setEntityRecords(result.entities);
      setEntityListSummary({
        count: result.count,
        limited: result.limited,
        type_counts: result.type_counts ?? {},
      });
    } catch (error) {
      showTrainingStatus({ type: "error", message: errorMessage(error) });
    }
  }, [debouncedEntitySearch, entityPage, entityTypeFilter, showTrainingStatus, source.id, tableName]);

  useEffect(() => {
    void refreshEntities();
  }, [refreshEntities]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedEntitySearch(entitySearch.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [entitySearch]);

  useEffect(() => {
    setEntityCandidates([]);
    setEntityColumn("");
    setEntityType("");
    setEntitySupportColumns([]);
    setQueuedEntityImport(null);
    setEntityListExpanded(false);
    setEntitySearch("");
    setDebouncedEntitySearch("");
    setEntityTypeFilter("all");
    setEntityPage(1);
    setEntityListSummary({ count: 0, limited: false, type_counts: {} });
    showTrainingStatus(null);
  }, [showTrainingStatus, tableName]);

  const loadEntityCandidates = useCallback(async () => {
    if (!source.id || !tableName) {
      showTrainingStatus({ type: "error", message: "请先选择数据库表。" });
      return;
    }
    setTrainingBusy(true);
    showTrainingStatus(null);
    try {
      const candidates = await listKnowledgeDatabaseSourceVannaEntityCandidates(source.id, {
        table_name: tableName,
        max_candidates: 12,
      });
      setEntityCandidates(candidates);
      if (candidates[0]) {
        setEntityColumn(candidates[0].column);
        setEntityType(candidates[0].suggested_entity_type);
        setEntitySupportColumns([]);
      }
      showTrainingStatus({ type: "success", message: `识别到 ${candidates.length} 个实体候选字段。` });
    } catch (error) {
      showTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [showTrainingStatus, source.id, tableName]);

  const normalizedEntitySupportColumns = useMemo(
    () => entitySupportColumns.filter((item) => item && item !== entityColumn).sort(),
    [entityColumn, entitySupportColumns]
  );
  const currentEntityImportKey = useMemo(
    () => `${tableName}:${entityColumn.trim()}:${entityType.trim()}:${normalizedEntitySupportColumns.join("|")}`,
    [entityColumn, entityType, normalizedEntitySupportColumns, tableName]
  );
  const queuedEntityImportKey = queuedEntityImport
    ? `${queuedEntityImport.tableName}:${queuedEntityImport.column}:${queuedEntityImport.entityType}:${queuedEntityImport.supportColumnsKey}`
    : "";
  const entityImportQueued = Boolean(queuedEntityImport?.jobId && queuedEntityImportKey === currentEntityImportKey);

  const importEntities = useCallback(async () => {
    if (!source.id) return;
    if (entityImportQueued) {
      return;
    }
    setTrainingBusy(true);
    showTrainingStatus(null);
    try {
      const supportColumns = normalizedEntitySupportColumns;
      const result = await importKnowledgeDatabaseSourceVannaEntities(source.id, {
        table_name: tableName,
        column: entityColumn,
        entity_type: entityType,
        alias_columns: supportColumns,
      });
      const jobId = result.job_id || result.job?.id || null;
      if (jobId) {
        setQueuedEntityImport({
          jobId,
          tableName,
          column: entityColumn.trim(),
          entityType: entityType.trim(),
          supportColumnsKey: supportColumns.join("|"),
        });
      }
      if (!jobId) {
        showTrainingStatus({ type: "success", message: "实体导入任务已创建。" });
      }
      window.setTimeout(() => {
        void refreshEntities();
      }, 3000);
    } catch (error) {
      showTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [
    entityColumn,
    entityImportQueued,
    entityType,
    normalizedEntitySupportColumns,
    queuedEntityImport?.jobId,
    refreshEntities,
    showTrainingStatus,
    source.id,
    tableName,
  ]);

  const deleteEntityRecord = useCallback(
    async (record: VannaEntityRecord) => {
      if (!source.id) return;
      const entityId = String(record.pk ?? record.id ?? "");
      if (!entityId) return;
      setTrainingBusy(true);
      showTrainingStatus(null);
      try {
        await deleteKnowledgeDatabaseSourceVannaEntity(source.id, entityId);
        showTrainingStatus({ type: "success", message: "实体已删除。" });
        await refreshEntities();
      } catch (error) {
        showTrainingStatus({ type: "error", message: errorMessage(error) });
      } finally {
        setTrainingBusy(false);
      }
    },
    [refreshEntities, showTrainingStatus, source.id]
  );

  const trainDdl = useCallback(async () => {
    if (!source.id || !tableName) return;
    setTrainingBusy(true);
    showTrainingStatus(null);
    try {
      const result = await trainKnowledgeDatabaseSourceVanna(source.id, {
        training_type: "ddl",
        table_name: tableName,
        table_names: [tableName],
      });
      showTrainingStatus({ type: "success", message: result.message || `${tableName} 的表结构已同步到 Vanna。` });
      await refreshTrainingData();
      refreshTrainingDataSoon();
    } catch (error) {
      showTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [refreshTrainingData, refreshTrainingDataSoon, showTrainingStatus, source.id, tableName]);

  const trainDocumentation = useCallback(async () => {
    if (!source.id || !tableName) return;
    setTrainingBusy(true);
    showTrainingStatus(null);
    try {
      const result = await trainKnowledgeDatabaseSourceVanna(source.id, {
        training_type: "documentation",
        table_name: tableName,
        documentation,
      });
      setDocumentation("");
      showTrainingStatus({ type: "success", message: result.message || `${tableName} 的业务说明已写入 Vanna。` });
      await refreshTrainingData();
      refreshTrainingDataSoon();
    } catch (error) {
      showTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [documentation, refreshTrainingData, refreshTrainingDataSoon, showTrainingStatus, source.id, tableName]);

  const trainSqlExample = useCallback(async () => {
    if (!source.id || !tableName) return;
    setTrainingBusy(true);
    showTrainingStatus(null);
    try {
      const result = await trainKnowledgeDatabaseSourceVanna(source.id, {
        training_type: "sql",
        table_name: tableName,
        question: sqlQuestion,
        sql: sqlExample,
      });
      setSqlQuestion("");
      setSqlExample("");
      showTrainingStatus({ type: "success", message: result.message || `${tableName} 的 SQL 示例已写入 Vanna。` });
      await refreshTrainingData();
      refreshTrainingDataSoon();
    } catch (error) {
      showTrainingStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setTrainingBusy(false);
    }
  }, [refreshTrainingData, refreshTrainingDataSoon, showTrainingStatus, source.id, sqlExample, sqlQuestion, tableName]);

  const deleteTrainingRecord = useCallback(
    async (record: VannaTrainingRecord) => {
      if (!source.id) return;
      setTrainingBusy(true);
      showTrainingStatus(null);
      try {
        await deleteKnowledgeDatabaseSourceVannaTraining(source.id, record.id);
        showTrainingStatus({ type: "success", message: "训练资料已删除。" });
        await refreshTrainingData();
        refreshTrainingDataSoon();
      } catch (error) {
        showTrainingStatus({ type: "error", message: errorMessage(error) });
      } finally {
        setTrainingBusy(false);
      }
    },
    [refreshTrainingData, refreshTrainingDataSoon, showTrainingStatus, source.id]
  );

  const trainingRecords = trainingData?.records ?? [];
  const ddlTrainingRecords = trainingRecords.filter((record) => vannaTrainingKind(record) === "ddl");
  const sqlTrainingRecords = trainingRecords.filter((record) => vannaTrainingKind(record) === "sql");
  const documentationTrainingRecords = trainingRecords.filter((record) => vannaTrainingKind(record) === "documentation");
  const trainingCounts = {
    ddl: ddlTrainingRecords.length,
    sql: sqlTrainingRecords.length,
    documentation: documentationTrainingRecords.length,
  };
  const entityTypeCounts = entityListSummary.type_counts ?? {};
  const scopedEntityCount =
    Object.values(entityTypeCounts).reduce((sum, value) => sum + (Number.isFinite(value) ? Number(value) : 0), 0) ||
    entityListSummary.count ||
    entityRecords.length;
  const entityTypeOptions = (
    Object.keys(entityTypeCounts).length > 0
      ? Object.keys(entityTypeCounts)
      : Array.from(new Set(entityRecords.map((record) => record.entity_type).filter(Boolean)))
  ).sort();
  const entityTopKTypes = Array.from(new Set([...entityTypeOptions, entityType.trim()].filter(Boolean))).sort();
  const normalizedEntitySearch = entitySearch.trim().toLowerCase();
  const getEntityAliases = (record: VannaEntityRecord): string[] =>
    Array.isArray(record.aliases) ? record.aliases.filter(Boolean).map(String) : [];
  const filteredEntityCount =
    normalizedEntitySearch || entityTypeFilter !== "all"
      ? entityListSummary.count
      : scopedEntityCount;
  const entityTotalPages = Math.max(1, Math.ceil(filteredEntityCount / ENTITY_PAGE_SIZE));
  const safeEntityPage = Math.min(entityPage, entityTotalPages);
  const visiblePagedEntityRecords = entityRecords;
  const getVisibleEntityAliases = (record: VannaEntityRecord): string[] => {
    const aliases = getEntityAliases(record);
    return aliases.slice(0, 5);
  };
  const entitySupportOptions = entityCandidates.map((candidate) => candidate.column).filter((column) => column !== entityColumn);
  const currentTrainingKey = `${source.id}:${tableName}`;
  const initialTrainingLoading = loadedTrainingKey !== currentTrainingKey;

  useEffect(() => {
    setEntityPage(1);
  }, [entitySearch, entityTypeFilter, tableName]);

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="flex max-h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-[#002fa7]">Vanna 表训练</p>
            <h3 className="mt-1 truncate text-lg font-semibold text-gray-950">{tableName}</h3>
            <p className="mt-1 text-sm text-gray-500">
              来源：{source.name} · 连接只是归属，DDL、实体字典和 SQL 示例按表维护。
            </p>
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
          <div className="rounded-3xl border border-[#002fa7]/10 bg-[#002fa7]/[0.025] p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-gray-950">当前数据库表</p>
                <p className="mt-1 text-xs text-gray-500">切换表后，下方训练资料和实体字典会按表刷新。</p>
              </div>
              <button
                type="button"
                onClick={refreshTrainingData}
                disabled={trainingLoading || !source.id}
                className="inline-flex h-9 items-center gap-2 rounded-full bg-white px-3 text-xs font-semibold text-[#002fa7] shadow-sm ring-1 ring-black/[0.05] transition hover:bg-[#002fa7]/[0.04] disabled:cursor-wait disabled:opacity-45"
              >
                {trainingLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                刷新
              </button>
            </div>
            <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
              {availableTables.map((table) => (
                <button
                  key={table}
                  type="button"
                  onClick={() => onTableChange(table)}
                  className={`shrink-0 rounded-2xl px-3 py-2 text-xs font-semibold transition ${
                    tableName === table
                      ? "bg-[#002fa7] text-white shadow-sm"
                      : "bg-white text-gray-600 shadow-sm ring-1 ring-black/[0.04] hover:bg-[#002fa7]/[0.06] hover:text-[#002fa7]"
                  }`}
                  title={table}
                >
                  {table}
                </button>
              ))}
            </div>
          </div>

          {trainingStatus ? (
            <div
              className={`mt-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl border px-4 py-3 text-sm ${
                trainingStatus.type === "success"
                  ? "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                  : "border-red-500/15 bg-red-50 text-red-600"
              }`}
            >
              <div className="flex min-w-0 items-start gap-2">
                {trainingStatus.type === "success" ? (
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                ) : (
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                )}
                <span>{trainingStatus.message}</span>
              </div>
              {trainingStatus.jobId ? (
                <Link
                  href={`/knowledge/imports/${encodeURIComponent(trainingStatus.jobId)}`}
                  className="inline-flex h-8 shrink-0 items-center justify-center rounded-full bg-[#002fa7] px-3 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a]"
                >
                  查看任务
                </Link>
              ) : null}
            </div>
          ) : null}

          {initialTrainingLoading ? (
            <div className="mt-4 flex min-h-[520px] flex-col items-center justify-center rounded-3xl border border-dashed border-[#002fa7]/15 bg-[#002fa7]/[0.025] px-6 text-center">
              <Loader2 className="h-9 w-9 animate-spin text-[#002fa7]" />
              <p className="mt-4 text-sm font-semibold text-gray-900">正在加载表训练数据</p>
              <p className="mt-2 text-xs text-gray-400">
                正在读取 {tableName} 的 DDL、SQL 示例、业务说明和实体字典。
              </p>
            </div>
          ) : (
            <>
          <div className="mt-4 grid gap-3 sm:grid-cols-3">
            <MetricCard icon={Database} title="DDL" value={trainingCounts.ddl ?? 0} tone="blue" compact />
            <MetricCard icon={CheckCircle2} title="SQL 示例" value={trainingCounts.sql ?? 0} tone="green" compact />
            <MetricCard icon={BookOpenText} title="业务说明" value={trainingCounts.documentation ?? 0} tone="orange" compact />
          </div>

          <div className="mt-4 rounded-3xl border border-black/[0.06] bg-white p-4 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-gray-950">导入表结构</p>
                <p className="mt-1 text-xs text-gray-400">把当前表的字段结构导入 Vanna。</p>
              </div>
              <button
                type="button"
                onClick={trainDdl}
                disabled={trainingBusy || !source.id || !tableName}
                className="inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
              >
                {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Database className="h-3.5 w-3.5" />}
                导入 DDL
              </button>
            </div>
            <div className="mt-4 border-t border-black/[0.06] pt-3">
              <div className="flex items-center justify-between gap-3">
                <p className="text-xs font-semibold text-gray-500">已导入表结构</p>
                <span className="text-[11px] text-gray-400">{ddlTrainingRecords.length} 条</span>
              </div>
              <div className="mt-2 space-y-2">
                {ddlTrainingRecords.length > 0 ? (
                  ddlTrainingRecords.map((record) => {
                    const content = record.content || record.preview || "";
                    return (
                      <div key={record.id} className="rounded-2xl bg-gray-50 px-3 py-3 ring-1 ring-black/[0.03]">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="rounded-full bg-[#002fa7]/10 px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">
                                表结构
                              </span>
                              <span className="truncate text-xs font-semibold text-gray-800">当前表结构</span>
                            </div>
                            <pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap break-words rounded-2xl bg-white px-3 py-2 font-mono text-[11px] leading-5 text-gray-600 ring-1 ring-black/[0.04]">
                              {content}
                            </pre>
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
                      </div>
                    );
                  })
                ) : (
                  <p className="rounded-2xl bg-gray-50 px-3 py-4 text-center text-xs text-gray-400">还没有导入表结构。</p>
                )}
              </div>
            </div>
          </div>

          <div className="mt-4 rounded-3xl border border-black/[0.06] bg-white p-4 shadow-sm">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-gray-950">实体字典</p>
                <p className="mt-1 text-xs text-gray-400">为当前表导入标准值，并用同一行的相关字段辅助匹配自然语言里的业务词。</p>
              </div>
              <button
                type="button"
                onClick={loadEntityCandidates}
                disabled={trainingBusy || !source.id || !tableName}
                className="inline-flex h-9 items-center gap-2 rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.06] px-3 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/10 disabled:cursor-not-allowed disabled:opacity-45"
              >
                {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                识别候选
              </button>
            </div>

            <div className="mt-3 grid gap-3 md:grid-cols-2">
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
                  placeholder="例如：product / region / organization"
                  className="h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-xs font-semibold text-gray-500">辅助匹配字段（可选）</span>
                <div className="min-h-10 rounded-2xl border border-black/[0.08] bg-white px-2 py-2">
                  {entitySupportOptions.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5">
                      {entitySupportOptions.map((column) => {
                        const active = entitySupportColumns.includes(column);
                        return (
                          <button
                            key={column}
                            type="button"
                            onClick={() =>
                              setEntitySupportColumns((current) =>
                                current.includes(column)
                                  ? current.filter((item) => item !== column)
                                  : [...current, column]
                              )
                            }
                            className={`rounded-xl px-2.5 py-1.5 text-xs font-semibold transition ${
                              active
                                ? "bg-[#002fa7] text-white"
                                : "bg-gray-50 text-gray-500 hover:bg-gray-100 hover:text-gray-700"
                            }`}
                          >
                            {column}
                          </button>
                        );
                      })}
                    </div>
                  ) : (
                    <p className="px-1 py-1.5 text-xs text-gray-400">先识别候选后选择；会自动排除当前实体字段。</p>
                  )}
                </div>
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
                      setEntitySupportColumns((current) => current.filter((item) => item !== candidate.column));
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

            <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
              {entityImportQueued && queuedEntityImport?.jobId ? (
                <Link
                  href={`/knowledge/imports/${encodeURIComponent(queuedEntityImport.jobId)}`}
                  className="inline-flex h-9 items-center gap-2 rounded-2xl border border-[#002fa7]/15 bg-white px-4 text-xs font-semibold text-[#002fa7] shadow-sm transition hover:bg-[#002fa7]/[0.04]"
                >
                  查看任务
                </Link>
              ) : null}
              <button
                type="button"
                onClick={importEntities}
                disabled={trainingBusy || entityImportQueued || !source.id || !tableName || !entityColumn.trim() || !entityType.trim()}
                className="inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
              >
                {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                {entityImportQueued ? "已进入队列" : "导入实体"}
              </button>
            </div>

            {entityImportQueued && queuedEntityImport?.jobId ? (
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-emerald-500/15 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                <span className="inline-flex items-center gap-2 font-semibold">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  实体导入任务已进入队列
                </span>
                <span className="text-emerald-700/70">可在任务详情查看进度，当前参数不会重复提交。</span>
              </div>
            ) : null}

            <div className="mt-4 rounded-2xl border border-[#002fa7]/10 bg-[#002fa7]/[0.025] p-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold text-gray-600">召回配置</p>
                  <p className="mt-1 text-[11px] text-gray-400">
                    SQL 生成前，每种实体类型带入多少个候选；没有单独设置的类型使用默认值。
                  </p>
                </div>
                <button
                  type="button"
                  onClick={saveEntityTopKSettings}
                  disabled={entityTopKSaving}
                  className="inline-flex h-8 items-center gap-1.5 rounded-2xl bg-[#002fa7] px-3 text-[11px] font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-wait disabled:opacity-50"
                >
                  {entityTopKSaving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                  {entityTopKSaving ? "保存中" : "保存配置"}
                </button>
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <label className="inline-flex items-center gap-2 rounded-2xl bg-white px-3 py-2 text-xs font-semibold text-gray-600 ring-1 ring-black/[0.04]">
                  默认每类
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={entityTopKDefault}
                    onChange={(event) => setEntityTopKDefault(event.target.value)}
                    className="h-7 w-16 rounded-xl border border-black/[0.06] bg-gray-50 px-2 text-xs font-semibold text-gray-900 outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                  />
                </label>
                {entityTopKTypes.length > 0 ? (
                  entityTopKTypes.map((type) => (
                    <label
                      key={type}
                      className="inline-flex items-center gap-2 rounded-2xl bg-white px-3 py-2 text-xs font-semibold text-gray-600 ring-1 ring-black/[0.04]"
                    >
                      {type}
                      <input
                        type="number"
                        min={1}
                        max={100}
                        value={entityTopKByType[type] ?? ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setEntityTopKByType((current) => {
                            if (value === "") {
                              const next = { ...current };
                              delete next[type];
                              return next;
                            }
                            return { ...current, [type]: value };
                          });
                        }}
                        placeholder={entityTopKDefault || "10"}
                        className="h-7 w-16 rounded-xl border border-black/[0.06] bg-gray-50 px-2 text-xs font-semibold text-gray-900 outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                      />
                    </label>
                  ))
                ) : (
                  <span className="text-[11px] text-gray-400">识别或导入实体后，可按类型单独设置。</span>
                )}
              </div>
            </div>

            <div className="mt-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold text-gray-500">当前表实体</p>
                  <p className="mt-1 text-[11px] text-gray-400">
                    已导入 {scopedEntityCount} 个；默认收起，按类型或关键词筛选后查看。
                  </p>
                </div>
              </div>
              {scopedEntityCount > 0 ? (
                <div className="mt-3 rounded-2xl bg-gray-50 p-3">
                  <div className="flex flex-col gap-2 md:flex-row md:items-center">
                    <input
                      value={entitySearch}
                      onChange={(event) => setEntitySearch(event.target.value)}
                      onFocus={() => setEntityListExpanded(true)}
                      placeholder="搜索实体名称"
                      className="h-9 flex-1 rounded-2xl border border-black/[0.06] bg-white px-3 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    />
                    <div className="flex min-w-0 items-center gap-1.5 overflow-x-auto pb-1 md:max-w-[52%] md:pb-0">
                      <button
                        type="button"
                        onClick={() => setEntityListExpanded((value) => !value)}
                        className={`shrink-0 rounded-xl px-2.5 py-1.5 text-xs font-semibold transition ${
                          entityListExpanded
                            ? "bg-[#002fa7] text-white"
                            : "bg-white text-gray-500 ring-1 ring-black/[0.04] hover:bg-[#002fa7]/[0.06] hover:text-[#002fa7]"
                        }`}
                      >
                        {entityListExpanded ? "收起" : "展开"}
                      </button>
                      <span className="h-5 w-px shrink-0 bg-black/[0.06]" />
                      {["all", ...entityTypeOptions].map((type) => {
                        const active = entityTypeFilter === type;
                        return (
                          <button
                            key={type}
                            type="button"
                            onClick={() => {
                              setEntityTypeFilter(type);
                              setEntityListExpanded(true);
                            }}
                            className={`shrink-0 rounded-xl px-2.5 py-1.5 text-xs font-semibold transition ${
                              active
                                ? "bg-[#002fa7] text-white"
                                : "bg-white text-gray-500 ring-1 ring-black/[0.04] hover:bg-[#002fa7]/[0.06] hover:text-[#002fa7]"
                            }`}
                          >
                            {type === "all" ? "全部类型" : type}
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  {entityListExpanded ? (
                    <div className="mt-3">
                      <div className="flex items-center justify-between gap-3 text-[11px] text-gray-400">
                        <span>
                          匹配 {filteredEntityCount} 个
                          {filteredEntityCount !== scopedEntityCount ? ` / 共 ${scopedEntityCount} 个` : ""}
                          {entitySearch.trim() ? ` · 筛选：${entitySearch.trim()}` : ""}
                        </span>
                        <span>
                          第 {safeEntityPage} / {entityTotalPages} 页
                        </span>
                      </div>
                      <div className="mt-2 space-y-2">
                        {visiblePagedEntityRecords.length > 0 ? (
                          visiblePagedEntityRecords.map((record) => {
                            const visibleAliases = getVisibleEntityAliases(record);
                            return (
                              <div
                                key={String(record.pk ?? record.id ?? `${record.table_column}-${record.canonical_name}`)}
                                className="flex items-start justify-between gap-3 rounded-2xl bg-white px-3 py-2 ring-1 ring-black/[0.03]"
                              >
                                <div className="min-w-0">
                                  <div className="flex flex-wrap items-center gap-2">
                                    <span className="rounded-full bg-[#002fa7]/10 px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">
                                      {record.entity_type}
                                    </span>
                                    <span className="truncate text-xs font-semibold text-gray-800">{record.canonical_name}</span>
                                  </div>
                                  <p className="mt-1 line-clamp-1 text-[11px] text-gray-400">
                                    {record.table_column}
                                    {visibleAliases.length ? ` · 辅助词：${visibleAliases.join("、")}` : ""}
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
                            );
                          })
                        ) : (
                          <p className="rounded-2xl bg-white px-3 py-4 text-center text-xs text-gray-400">没有匹配的实体。</p>
                        )}
                      </div>
                      {filteredEntityCount > ENTITY_PAGE_SIZE ? (
                        <div className="mt-3 flex items-center justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => setEntityPage((page) => Math.max(1, page - 1))}
                            disabled={safeEntityPage <= 1}
                            className="h-8 rounded-full bg-white px-3 text-xs font-semibold text-gray-500 ring-1 ring-black/[0.04] transition hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            上一页
                          </button>
                          <button
                            type="button"
                            onClick={() => setEntityPage((page) => Math.min(entityTotalPages, page + 1))}
                            disabled={safeEntityPage >= entityTotalPages}
                            className="h-8 rounded-full bg-white px-3 text-xs font-semibold text-gray-500 ring-1 ring-black/[0.04] transition hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            下一页
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : (
                <p className="mt-2 rounded-2xl bg-gray-50 px-3 py-4 text-center text-xs text-gray-400">当前表还没有导入实体。</p>
              )}
            </div>
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <div className="flex h-full flex-col rounded-3xl border border-black/[0.06] bg-white p-4 shadow-sm">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-semibold text-gray-950">SQL 示例</p>
                <span className="text-xs text-gray-400">{sqlTrainingRecords.length} 条</span>
              </div>
              <div className="mt-3 flex min-h-[160px] flex-col gap-2">
                <input
                  value={sqlQuestion}
                  onChange={(event) => setSqlQuestion(event.target.value)}
                  placeholder="例：按当前表统计配置率"
                  className="h-10 w-full rounded-2xl border border-black/[0.08] px-3 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
                <textarea
                  value={sqlExample}
                  onChange={(event) => setSqlExample(event.target.value)}
                  placeholder="SELECT ..."
                  className="min-h-0 flex-1 resize-none rounded-2xl border border-black/[0.08] px-3 py-2 font-mono text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
              </div>
              <button
                type="button"
                onClick={trainSqlExample}
                disabled={trainingBusy || !source.id || !sqlQuestion.trim() || !sqlExample.trim()}
                className="mt-3 inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
              >
                {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
                保存示例
              </button>
              <div className="mt-auto flex items-center justify-between gap-3 pt-3">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-xs font-semibold text-gray-500">已保存示例</p>
                  <span className="text-[11px] text-gray-400">{sqlTrainingRecords.length} 条</span>
                </div>
                <button
                  type="button"
                  onClick={() => setSavedRecordsDialog("sql")}
                  className="inline-flex h-8 items-center rounded-full bg-gray-50 px-3 text-[11px] font-semibold text-[#002fa7] ring-1 ring-black/[0.04] transition hover:bg-[#002fa7]/[0.06]"
                >
                  查看
                </button>
              </div>
            </div>

            <div className="flex h-full flex-col rounded-3xl border border-black/[0.06] bg-white p-4 shadow-sm">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-semibold text-gray-950">业务说明</p>
                <span className="text-xs text-gray-400">{documentationTrainingRecords.length} 条</span>
              </div>
              <div className="mt-3 flex min-h-[160px] flex-col">
                <textarea
                  value={documentation}
                  onChange={(event) => setDocumentation(event.target.value)}
                  placeholder="写当前表的字段含义、业务口径、常见过滤条件等。"
                  className="min-h-0 flex-1 resize-none rounded-2xl border border-black/[0.08] px-3 py-2 text-xs outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
              </div>
              <button
                type="button"
                onClick={trainDocumentation}
                disabled={trainingBusy || !source.id || !documentation.trim()}
                className="mt-3 inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
              >
                {trainingBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BookOpenText className="h-3.5 w-3.5" />}
                保存说明
              </button>
              <div className="mt-auto flex items-center justify-between gap-3 pt-3">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-xs font-semibold text-gray-500">已保存说明</p>
                  <span className="text-[11px] text-gray-400">{documentationTrainingRecords.length} 条</span>
                </div>
                <button
                  type="button"
                  onClick={() => setSavedRecordsDialog("documentation")}
                  className="inline-flex h-8 items-center rounded-full bg-gray-50 px-3 text-[11px] font-semibold text-[#002fa7] ring-1 ring-black/[0.04] transition hover:bg-[#002fa7]/[0.06]"
                >
                  查看
                </button>
              </div>
            </div>
          </div>
            </>
          )}
        </div>
      </div>
      {savedRecordsDialog ? (
        <SavedTrainingRecordsDialog
          kind={savedRecordsDialog}
          records={savedRecordsDialog === "sql" ? sqlTrainingRecords : documentationTrainingRecords}
          busy={trainingBusy}
          onDelete={deleteTrainingRecord}
          onClose={() => setSavedRecordsDialog(null)}
        />
      ) : null}
      {actionDialog ? <ActionFeedbackDialog dialog={actionDialog} onClose={() => setActionDialog(null)} /> : null}
    </div>
  );
}

function ActionFeedbackDialog({ dialog, onClose }: { dialog: ActionDialog; onClose: () => void }) {
  const isSuccess = dialog.type === "success";
  const Icon = isSuccess ? CheckCircle2 : AlertCircle;

  return (
    <div className="fixed inset-0 z-[160] flex items-center justify-center bg-black/30 px-4 py-6">
      <div className="w-full max-w-sm overflow-hidden rounded-[24px] bg-white p-5 shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start gap-3">
          <div
            className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl ${
              isSuccess ? "bg-emerald-50 text-emerald-600" : "bg-red-50 text-red-600"
            }`}
          >
            <Icon className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-semibold text-gray-950">{dialog.title}</h3>
            <p className="mt-1 text-sm leading-6 text-gray-500">{dialog.message}</p>
          </div>
        </div>
        <div className="mt-5 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-10 items-center rounded-2xl bg-[#002fa7] px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-[#001f7a]"
          >
            知道了
          </button>
        </div>
      </div>
    </div>
  );
}

function SavedTrainingRecordsDialog({
  kind,
  records,
  busy,
  onDelete,
  onClose,
}: {
  kind: "sql" | "documentation";
  records: VannaTrainingRecord[];
  busy: boolean;
  onDelete: (record: VannaTrainingRecord) => void;
  onClose: () => void;
}) {
  const pageSize = 10;
  const [page, setPage] = useState(1);
  const totalPages = Math.max(1, Math.ceil(records.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const start = (safePage - 1) * pageSize;
  const pageRecords = records.slice(start, start + pageSize);
  const title = kind === "sql" ? "已保存 SQL 示例" : "已保存业务说明";

  useEffect(() => {
    setPage(1);
  }, [kind]);

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages);
    }
  }, [page, totalPages]);

  return (
    <div className="fixed inset-0 z-[150] flex items-center justify-center bg-black/30 px-4 py-6">
      <div className="flex max-h-[82vh] w-full max-w-3xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-[#002fa7]">Vanna 训练资料</p>
            <h3 className="mt-1 text-lg font-semibold text-gray-950">{title}</h3>
            <p className="mt-1 text-sm text-gray-500">共 {records.length} 条，每页 {pageSize} 条。</p>
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
          {pageRecords.length > 0 ? (
            <div className="space-y-3">
              {pageRecords.map((record, index) => {
                const content = record.content || record.preview || "";
                return (
                  <div key={record.id} className="rounded-2xl bg-gray-50 px-4 py-3 ring-1 ring-black/[0.03]">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="rounded-full bg-[#002fa7]/10 px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">
                            #{start + index + 1}
                          </span>
                          {kind === "sql" ? (
                            <span className="truncate text-xs font-semibold text-gray-800">{record.question || "SQL 示例"}</span>
                          ) : (
                            <span className="text-xs font-semibold text-gray-800">业务说明</span>
                          )}
                        </div>
                        {kind === "sql" ? (
                          <pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-white px-3 py-2 font-mono text-[11px] leading-5 text-gray-600 ring-1 ring-black/[0.04]">
                            {content}
                          </pre>
                        ) : (
                          <p className="mt-2 whitespace-pre-wrap break-words text-xs leading-5 text-gray-600">{content}</p>
                        )}
                      </div>
                      <button
                        type="button"
                        onClick={() => onDelete(record)}
                        disabled={busy}
                        className="shrink-0 rounded-full px-2 py-1 text-[11px] font-semibold text-gray-400 transition hover:bg-red-50 hover:text-red-600 disabled:cursor-wait disabled:opacity-40"
                      >
                        删除
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="rounded-2xl bg-gray-50 px-4 py-8 text-center text-sm text-gray-400">
              {kind === "sql" ? "还没有 SQL 示例。" : "还没有业务说明。"}
            </p>
          )}
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-black/[0.06] px-6 py-4">
          <span className="text-xs text-gray-400">
            第 {safePage} / {totalPages} 页
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPage((current) => Math.max(1, current - 1))}
              disabled={safePage <= 1}
              className="h-8 rounded-full bg-gray-50 px-3 text-xs font-semibold text-gray-600 ring-1 ring-black/[0.04] transition hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40"
            >
              上一页
            </button>
            <button
              type="button"
              onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
              disabled={safePage >= totalPages}
              className="h-8 rounded-full bg-gray-50 px-3 text-xs font-semibold text-gray-600 ring-1 ring-black/[0.04] transition hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      </div>
    </div>
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
