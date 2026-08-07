"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  AlertCircle,
  BookOpenText,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  FileText,
  FileSpreadsheet,
  Folder,
  FolderOpen,
  Layers3,
  ListTodo,
  Loader2,
  MoreHorizontal,
  RefreshCw,
  Search,
  ShieldCheck,
  Sigma,
  Trash2,
  Upload,
  X,
  type LucideIcon,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import WorkspacePageHeader from "@/components/layout/WorkspacePageHeader";
import {
  databaseQueryResultExportCsvUrl,
  createAnalyticsModel,
  createConcatDataset,
  appendConcatDatasetSources,
  previewConcatDataset,
  createSemanticAsset,
  deleteKnowledgeDatabaseSource,
  generateTableAssetProfile,
  getAnalyticsModel,
  getDatabaseQueryResultPage,
  getKnowledgeImportJob,
  getSemanticAsset,
  getSemanticDimensionMatching,
  getSemanticDimensionMatchingOverview,
  getSemanticDimensionBaselineChange,
  getSemanticDimensionBuildJob,
  getTableAsset,
  getTableAssetProfileJob,
  deleteKnowledgeDatabaseSourceVannaEntity,
  deleteKnowledgeDatabaseSourceVannaTraining,
  analyticsProjectExportDownloadUrl,
  importAnalyticsModels,
  importKnowledgeDatabaseSourceVannaEntities,
  importSemanticAssets,
  listAnalyticsModels,
  listTableAssetEntityCandidates,
  listKnowledgeDatabaseSourceVannaEntities,
  listKnowledgeDatabaseSourceVannaEntityCandidates,
  listKnowledgeDatabaseSourceVannaTraining,
  listKnowledgeDatabaseSourceTableColumns,
  listDatabaseQueryResults,
  listKnowledgeDatabaseSourceTables,
  listKnowledgeDatabaseSources,
  listSemanticAssets,
  listSqlGuardrails,
  listSqlGuardrailTypes,
  listTableAssets,
  listTaskCenter,
  planAnalyticsProjectExport,
  readFile,
  refreshAnalyticsModels,
  refreshConcatDataset,
  refreshSemanticAssets,
  refreshTableAssetProfiles,
  removeTableAsset,
  updateLogicalDatasetDefinition,
  saveFile,
  saveSqlGuardrail,
  saveKnowledgeDatabaseSource,
  updateSemanticDimensionDefinition,
  updateSemanticRelationDefinition,
  saveSemanticDimensionOverride,
  saveSemanticDimensionEntityLifecycle,
  resolveSemanticDimensionBaselineChange,
  publishSemanticDimensionMatching,
  deleteSemanticDimensionOverride,
  deleteSqlGuardrail,
  resetSqlGuardrails,
  testKnowledgeDatabaseSource,
  trainKnowledgeDatabaseSourceVanna,
  type AnalyticsModelDetail,
  type AnalyticsModelFile,
  type AnalyticsModelSummary,
  type AnalyticsProjectDataFileMode,
  type AnalyticsProjectExportPlan,
  type AssetRelationDefinition,
  type ConcatDatasetPreview,
  type DatabaseQueryResultPage,
  type DatabaseQueryResultSummary,
  type DimensionDefinition,
  type KnowledgeDatabaseSource,
  type SemanticAssetDetail,
  type SemanticAssetFile,
  type SemanticAssetSummary,
  type SemanticAssetType,
  type SemanticDimensionUpdatePayload,
  type SemanticDimensionMatchingView,
  type SemanticDimensionMatchingOverview,
  type SemanticDimensionBaselineChange,
  type SemanticDimensionMatchRow,
  type SqlGuardrailAction,
  type SqlGuardrailActionType,
  type SqlGuardrailRule,
  type SqlGuardrailTypeDefinition,
  type TableAsset,
  type TableEntityCandidate,
  type TaskCenterItem,
  type TaskJobEvent,
  type VannaEntityListResult,
  type VannaEntityRecord,
  type VannaTrainingData,
  type VannaTrainingRecord,
} from "@/lib/api";
import { buildFileTree, fileDirectoryPaths, type FileTreeNode } from "@/lib/fileTree";
import { getSettings, updateSettings } from "@/lib/settingsApi";
import { useApp } from "@/lib/store";

type AnalyticsSection = "results" | "assets" | "models" | "measures" | "guardrails" | "tasks";
type TrainingStatus = { type: "success" | "error"; message: string; jobId?: string | null };
type ActionDialog = { type: "success" | "error"; title: string; message: string };
const ENTITY_PAGE_SIZE = 10;
const MATCHING_PAGE_SIZE = 50;
type QueuedEntityImport = {
  jobId: string;
  tableName: string;
  column: string;
  entityType: string;
  supportColumnsKey: string;
};
type TaskDetailState = {
  task: TaskCenterItem;
  job: Record<string, unknown>;
  events: TaskJobEvent[];
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
  if (asset.source_type === "logical_concat") return "逻辑数据集 · 虚拟纵向合并";
  if (asset.source_type === "derived_concat") return "逻辑数据集 · 已物化缓存";
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
  const [logicalDefinitionAsset, setLogicalDefinitionAsset] = useState<TableAsset | null>(null);
  const [logicalDefinitionEditor, setLogicalDefinitionEditor] = useState<TableAsset | null>(null);
  const [assetPendingRemoval, setAssetPendingRemoval] = useState<TableAsset | null>(null);
  const [concatDatasetModalOpen, setConcatDatasetModalOpen] = useState(false);
  const [concatDatasetBusy, setConcatDatasetBusy] = useState(false);
  const [concatAppendTarget, setConcatAppendTarget] = useState<TableAsset | null>(null);
  const [refreshingConcatDatasetId, setRefreshingConcatDatasetId] = useState<string | null>(null);
  const [removingAssetId, setRemovingAssetId] = useState<string | null>(null);
  const [databaseSourcePendingRemoval, setDatabaseSourcePendingRemoval] = useState<KnowledgeDatabaseSource | null>(null);
  const [removingDatabaseSourceId, setRemovingDatabaseSourceId] = useState<string | null>(null);
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
  const [toast, setToast] = useState<{ type: "success" | "warning" | "error"; message: string } | null>(null);
  const [actionDialog, setActionDialog] = useState<ActionDialog | null>(null);
  const [activeSection, setActiveSection] = useState<AnalyticsSection>("assets");
  const [queryResults, setQueryResults] = useState<DatabaseQueryResultSummary[]>([]);
  const [selectedQueryResultId, setSelectedQueryResultId] = useState("");
  const [queryResultPage, setQueryResultPage] = useState<DatabaseQueryResultPage | null>(null);
  const [queryResultsLoading, setQueryResultsLoading] = useState(false);
  const [queryResultPageLoading, setQueryResultPageLoading] = useState(false);
  const [queryResultPageNumber, setQueryResultPageNumber] = useState(1);
  const [queryResultPageSize, setQueryResultPageSize] = useState("100");
  const [taskCenterItems, setTaskCenterItems] = useState<TaskCenterItem[]>([]);
  const [taskCenterLoading, setTaskCenterLoading] = useState(false);
  const [taskDetail, setTaskDetail] = useState<TaskDetailState | null>(null);
  const [taskDetailLoading, setTaskDetailLoading] = useState(false);
  const [semanticAssets, setSemanticAssets] = useState<SemanticAssetSummary[]>([]);
  const [semanticAssetsLoading, setSemanticAssetsLoading] = useState(false);
  const [semanticAssetsBusy, setSemanticAssetsBusy] = useState(false);
  const [semanticAssetModal, setSemanticAssetModal] = useState<"create" | "import" | null>(null);
  const [semanticAssetSearch, setSemanticAssetSearch] = useState("");
  const [semanticAssetTypeFilter, setSemanticAssetTypeFilter] = useState<"all" | SemanticAssetType>("all");
  const [semanticAssetDetail, setSemanticAssetDetail] = useState<SemanticAssetDetail | null>(null);
  const [semanticAssetDetailLoading, setSemanticAssetDetailLoading] = useState(false);
  const [semanticAssetSelectedFile, setSemanticAssetSelectedFile] = useState<SemanticAssetFile | null>(null);
  const [semanticAssetEditorContent, setSemanticAssetEditorContent] = useState("");
  const [semanticAssetEditorOriginal, setSemanticAssetEditorOriginal] = useState("");
  const [semanticAssetEditorLoading, setSemanticAssetEditorLoading] = useState(false);
  const [semanticAssetEditorSaving, setSemanticAssetEditorSaving] = useState(false);
  const [semanticDimensionDefinitionEditor, setSemanticDimensionDefinitionEditor] = useState<SemanticAssetDetail | null>(null);
  const [semanticRelationDefinitionEditor, setSemanticRelationDefinitionEditor] = useState<SemanticAssetDetail | null>(null);
  const [semanticDimensionDefinitionInitialTab, setSemanticDimensionDefinitionInitialTab] = useState<"settings" | "matching" | "markdown">("settings");
  const [semanticDimensionDefinitionSaving, setSemanticDimensionDefinitionSaving] = useState(false);
  const [analyticsModels, setAnalyticsModels] = useState<AnalyticsModelSummary[]>([]);
  const [analyticsModelsLoading, setAnalyticsModelsLoading] = useState(false);
  const [analyticsModelsBusy, setAnalyticsModelsBusy] = useState(false);
  const [analyticsModelModal, setAnalyticsModelModal] = useState<"create" | "import" | null>(null);
  const [analyticsModelSearch, setAnalyticsModelSearch] = useState("");
  const [analyticsModelDetail, setAnalyticsModelDetail] = useState<AnalyticsModelDetail | null>(null);
  const [analyticsModelDetailLoading, setAnalyticsModelDetailLoading] = useState(false);
  const [analyticsModelSelectedFile, setAnalyticsModelSelectedFile] = useState<AnalyticsModelFile | null>(null);
  const [analyticsModelEditorContent, setAnalyticsModelEditorContent] = useState("");
  const [analyticsModelEditorOriginal, setAnalyticsModelEditorOriginal] = useState("");
  const [analyticsModelEditorLoading, setAnalyticsModelEditorLoading] = useState(false);
  const [analyticsModelEditorSaving, setAnalyticsModelEditorSaving] = useState(false);
  const [sqlGuardrails, setSqlGuardrails] = useState<SqlGuardrailRule[]>([]);
  const [sqlGuardrailTypes, setSqlGuardrailTypes] = useState<Record<string, SqlGuardrailTypeDefinition>>({});
  const [sqlGuardrailsLoading, setSqlGuardrailsLoading] = useState(false);
  const [sqlGuardrailBusy, setSqlGuardrailBusy] = useState(false);
  const [sqlGuardrailEditor, setSqlGuardrailEditor] = useState<SqlGuardrailRule | null>(null);

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
    void loadQueryResults();
  }, [loadQueryResults]);

  useEffect(() => {
    if (activeSection === "results" && selectedQueryResultId) {
      void loadQueryResultPage();
    }
  }, [activeSection, loadQueryResultPage, selectedQueryResultId]);

  const loadTaskCenter = useCallback(async () => {
    setTaskCenterLoading(true);
    try {
      setTaskCenterItems(await listTaskCenter(100));
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setTaskCenterLoading(false);
    }
  }, []);

  const openTaskDetail = useCallback(async (task: TaskCenterItem) => {
    const jobId = String(task.job.id || "").trim();
    if (!jobId) return;
    setTaskDetail({ task, job: task.job, events: [] });
    setTaskDetailLoading(true);
    try {
      if (task.task_type === "semantic_dimension_build") {
        const detail = await getSemanticDimensionBuildJob(jobId);
        setTaskDetail({ task, job: detail.job as unknown as Record<string, unknown>, events: detail.events });
      } else if (task.task_type === "knowledge_import") {
        const detail = await getKnowledgeImportJob(jobId, true);
        setTaskDetail({
          task,
          job: detail.job as unknown as Record<string, unknown>,
          events: (detail.events || []).map((event) => ({
            id: event.id,
            job_id: event.job_id,
            level: event.level,
            message: event.message,
            metadata: event.metadata,
            created_at: event.created_at,
          })),
        });
      }
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setTaskDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTaskCenter();
  }, [loadTaskCenter]);

  const loadSemanticAssets = useCallback(async (forceRefresh = false) => {
    setSemanticAssetsLoading(true);
    try {
      const result = forceRefresh ? await refreshSemanticAssets() : await listSemanticAssets();
      setSemanticAssets(result.assets);
      return true;
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
      return false;
    } finally {
      setSemanticAssetsLoading(false);
    }
  }, []);

  const handleRefreshSemanticAssets = useCallback(async () => {
    const ok = await loadSemanticAssets(true);
    if (ok) {
      setToast({ type: "success", message: "语义资产 registry 已刷新" });
    }
  }, [loadSemanticAssets]);

  const loadAnalyticsModels = useCallback(async (forceRefresh = false) => {
    setAnalyticsModelsLoading(true);
    try {
      const result = forceRefresh ? await refreshAnalyticsModels() : await listAnalyticsModels();
      setAnalyticsModels(result.models);
      return true;
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
      return false;
    } finally {
      setAnalyticsModelsLoading(false);
    }
  }, []);

  const handleRefreshAnalyticsModels = useCallback(async () => {
    const ok = await loadAnalyticsModels(true);
    if (ok) {
      setToast({ type: "success", message: "分析模型 registry 已刷新" });
    }
  }, [loadAnalyticsModels]);

  useEffect(() => {
    void loadAnalyticsModels();
  }, [loadAnalyticsModels]);

  useEffect(() => {
    if (activeSection === "measures") {
      void loadSemanticAssets();
    }
  }, [activeSection, loadSemanticAssets]);

  useEffect(() => {
    void loadSemanticAssets();
  }, [loadSemanticAssets]);

  useEffect(() => {
    if (activeSection === "models") {
      void loadAnalyticsModels();
      if (semanticAssets.length === 0) {
        void loadSemanticAssets();
      }
    }
  }, [activeSection, loadAnalyticsModels, loadSemanticAssets, semanticAssets.length]);

  const loadSqlGuardrails = useCallback(async () => {
    setSqlGuardrailsLoading(true);
    try {
      const [types, rules] = await Promise.all([listSqlGuardrailTypes(), listSqlGuardrails()]);
      setSqlGuardrailTypes(types);
      setSqlGuardrails(rules);
      return true;
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
      return false;
    } finally {
      setSqlGuardrailsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeSection === "guardrails") {
      void loadSqlGuardrails();
      if (semanticAssets.length === 0) {
        void loadSemanticAssets();
      }
    }
  }, [activeSection, loadSemanticAssets, loadSqlGuardrails, semanticAssets.length]);

  useEffect(() => {
    void loadSqlGuardrails();
  }, [loadSqlGuardrails]);

  const handleSaveSqlGuardrail = useCallback(
    async (rule: SqlGuardrailRule) => {
      setSqlGuardrailBusy(true);
      try {
        await saveSqlGuardrail(rule);
        await loadSqlGuardrails();
        setSqlGuardrailEditor(null);
        setActionDialog({
          type: "success",
          title: "SQL 守卫文档已保存",
          message: `规则“${rule.name}”已写入 guardrail.md，后端会从 frontmatter 编译执行。`,
        });
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setSqlGuardrailBusy(false);
      }
    },
    [loadSqlGuardrails]
  );

  const handleDeleteSqlGuardrail = useCallback(
    async (rule: SqlGuardrailRule) => {
      setSqlGuardrailBusy(true);
      try {
        await deleteSqlGuardrail(rule.id);
        await loadSqlGuardrails();
        setToast({ type: "success", message: `已删除 SQL 守卫：${rule.name}` });
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setSqlGuardrailBusy(false);
      }
    },
    [loadSqlGuardrails]
  );

  const handleResetSqlGuardrails = useCallback(async () => {
    setSqlGuardrailBusy(true);
    try {
      const rules = await resetSqlGuardrails();
      const types = Object.keys(sqlGuardrailTypes).length ? sqlGuardrailTypes : await listSqlGuardrailTypes();
      setSqlGuardrails(rules);
      setSqlGuardrailTypes(types);
      setToast({ type: "success", message: "SQL 守卫已恢复默认规则" });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setSqlGuardrailBusy(false);
    }
  }, [sqlGuardrailTypes]);

  const handleCreateSemanticAsset = useCallback(
    async (payload: {
      name: string;
      type: SemanticAssetType;
      description: string;
      aliases: string[];
      tags: string[];
      version: string;
      dimension_definition?: DimensionDefinition;
      relation_definition?: AssetRelationDefinition;
    }) => {
      setSemanticAssetsBusy(true);
      try {
        const asset = await createSemanticAsset(payload);
        await loadSemanticAssets();
        setSemanticAssetModal(null);
        setToast({ type: "success", message: `已创建语义资产：${asset.name}` });
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setSemanticAssetsBusy(false);
      }
    },
    [loadSemanticAssets]
  );

  const handleImportSemanticAssets = useCallback(
    async (files: File[]) => {
      if (files.length === 0) {
        setToast({ type: "error", message: "请选择 ZIP 或文件夹。" });
        return;
      }
      setSemanticAssetsBusy(true);
      try {
        const result = await importSemanticAssets(files);
        setSemanticAssets(result.assets);
        setSemanticAssetModal(null);
        setToast({ type: "success", message: `已导入 ${result.count} 个语义资产` });
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setSemanticAssetsBusy(false);
      }
    },
    []
  );

  const handleCreateAnalyticsModel = useCallback(
    async (payload: {
      name: string;
      description: string;
      tags: string[];
      version: string;
      data_assets: Record<string, unknown>;
      semantic_assets: Record<string, unknown>;
      guardrails: string[];
      templates: Record<string, unknown>;
      default_template: string | null;
    }) => {
      setAnalyticsModelsBusy(true);
      try {
        const model = await createAnalyticsModel(payload);
        await loadAnalyticsModels();
        setAnalyticsModelModal(null);
        setActionDialog({
          type: "success",
          title: "分析模型已创建",
          message: `已创建分析模型“${model.name}”，model.md 已写入 analytics-models 目录并刷新 registry。`,
        });
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setAnalyticsModelsBusy(false);
      }
    },
    [loadAnalyticsModels]
  );

  const handleImportAnalyticsModels = useCallback(
    async (files: File[]) => {
      if (files.length === 0) {
        setToast({ type: "error", message: "请选择 ZIP 或文件夹。" });
        return;
      }
      setAnalyticsModelsBusy(true);
      try {
        const result = await importAnalyticsModels(files);
        setAnalyticsModels(result.models);
        setAnalyticsModelModal(null);
        setActionDialog({
          type: "success",
          title: "分析模型已导入",
          message: `已导入 ${result.count} 个分析模型，registry 已刷新。`,
        });
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setAnalyticsModelsBusy(false);
      }
    },
    []
  );

  const openSemanticAssetDetail = useCallback(async (asset: SemanticAssetSummary) => {
    setSemanticAssetDetailLoading(true);
    setSemanticAssetDetail(null);
    setSemanticAssetSelectedFile(null);
    setSemanticAssetEditorContent("");
    setSemanticAssetEditorOriginal("");
    try {
      const detail = await getSemanticAsset(asset.id);
      if (asset.type === "dimension") {
        setSemanticDimensionDefinitionInitialTab("settings");
        setSemanticDimensionDefinitionEditor(detail);
        return;
      }
      setSemanticAssetDetail(detail);
      const mainFile = (detail.files || []).find((file) => file.main) || detail.files?.[0] || null;
      if (mainFile) {
        setSemanticAssetSelectedFile(mainFile);
        setSemanticAssetEditorLoading(true);
        const content = await readFile(mainFile.path);
        setSemanticAssetEditorContent(content);
        setSemanticAssetEditorOriginal(content);
      }
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setSemanticAssetDetailLoading(false);
      setSemanticAssetEditorLoading(false);
    }
  }, []);

  const openTaskDimensionMatching = useCallback(async (dimensionId: string) => {
    setTaskDetail(null);
    setActiveSection("measures");
    try {
      const assetId = `dimension:${dimensionId}`;
      const detail = await getSemanticAsset(assetId);
      setSemanticDimensionDefinitionInitialTab("matching");
      setSemanticDimensionDefinitionEditor(detail);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    }
  }, []);

  const openAnalyticsModelDetail = useCallback(async (model: AnalyticsModelSummary) => {
    setAnalyticsModelDetailLoading(true);
    setAnalyticsModelDetail(null);
    setAnalyticsModelSelectedFile(null);
    setAnalyticsModelEditorContent("");
    setAnalyticsModelEditorOriginal("");
    try {
      const detail = await getAnalyticsModel(model.id);
      setAnalyticsModelDetail(detail);
      const mainFile = (detail.files || []).find((file) => file.main) || detail.files?.[0] || null;
      if (mainFile) {
        setAnalyticsModelSelectedFile(mainFile);
        setAnalyticsModelEditorLoading(true);
        const content = await readFile(mainFile.path);
        setAnalyticsModelEditorContent(content);
        setAnalyticsModelEditorOriginal(content);
      }
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setAnalyticsModelDetailLoading(false);
      setAnalyticsModelEditorLoading(false);
    }
  }, []);

  const selectSemanticAssetFile = useCallback(async (file: SemanticAssetFile) => {
    if (!file.editable) {
      setToast({ type: "error", message: "这个文件类型暂不支持在线编辑。" });
      return;
    }
    setSemanticAssetSelectedFile(file);
    setSemanticAssetEditorLoading(true);
    try {
      const content = await readFile(file.path);
      setSemanticAssetEditorContent(content);
      setSemanticAssetEditorOriginal(content);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setSemanticAssetEditorLoading(false);
    }
  }, []);

  const selectAnalyticsModelFile = useCallback(async (file: AnalyticsModelFile) => {
    if (!file.editable) {
      setToast({ type: "error", message: "这个文件类型暂不支持在线编辑。" });
      return;
    }
    setAnalyticsModelSelectedFile(file);
    setAnalyticsModelEditorLoading(true);
    try {
      const content = await readFile(file.path);
      setAnalyticsModelEditorContent(content);
      setAnalyticsModelEditorOriginal(content);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setAnalyticsModelEditorLoading(false);
    }
  }, []);

  const saveSemanticAssetFile = useCallback(async () => {
    if (!semanticAssetSelectedFile) return;
    setSemanticAssetEditorSaving(true);
    try {
      await saveFile(semanticAssetSelectedFile.path, semanticAssetEditorContent);
      setSemanticAssetEditorOriginal(semanticAssetEditorContent);
      await loadSemanticAssets(true);
      if (semanticAssetDetail) {
        const detail = await getSemanticAsset(semanticAssetDetail.id);
        setSemanticAssetDetail(detail);
        const updatedFile = (detail.files || []).find((file) => file.path === semanticAssetSelectedFile.path) || semanticAssetSelectedFile;
        setSemanticAssetSelectedFile(updatedFile);
      }
      setToast({ type: "success", message: "语义资产文件已保存" });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setSemanticAssetEditorSaving(false);
    }
  }, [loadSemanticAssets, semanticAssetDetail, semanticAssetEditorContent, semanticAssetSelectedFile]);

  const saveSemanticDimensionDefinition = useCallback(async (payload: SemanticDimensionUpdatePayload) => {
    if (!semanticDimensionDefinitionEditor) return;
    setSemanticDimensionDefinitionSaving(true);
    try {
      await updateSemanticDimensionDefinition(semanticDimensionDefinitionEditor.id, payload);
      setSemanticDimensionDefinitionEditor(null);
      await loadSemanticAssets(true);
      setToast({ type: "success", message: "维度设置已保存，语义资产 registry 已刷新" });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setSemanticDimensionDefinitionSaving(false);
    }
  }, [loadSemanticAssets, semanticDimensionDefinitionEditor]);

  const saveSemanticDimensionMarkdown = useCallback(async (content: string) => {
    if (!semanticDimensionDefinitionEditor) return;
    setSemanticDimensionDefinitionSaving(true);
    try {
      await saveFile(semanticDimensionDefinitionEditor.path, content);
      setSemanticDimensionDefinitionEditor(null);
      await loadSemanticAssets(true);
      setToast({ type: "success", message: "维度 Markdown 已保存，语义资产 registry 已刷新" });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setSemanticDimensionDefinitionSaving(false);
    }
  }, [loadSemanticAssets, semanticDimensionDefinitionEditor]);

  const saveSemanticRelationDefinition = useCallback(async (payload: {
    name: string;
    description: string;
    aliases: string[];
    tags: string[];
    version: string;
    relation_definition: AssetRelationDefinition;
  }) => {
    if (!semanticRelationDefinitionEditor) return;
    setSemanticDimensionDefinitionSaving(true);
    try {
      await updateSemanticRelationDefinition(semanticRelationDefinitionEditor.id, payload);
      setSemanticRelationDefinitionEditor(null);
      await loadSemanticAssets(true);
      setToast({ type: "success", message: "资产关联已保存，语义资产 registry 已刷新" });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setSemanticDimensionDefinitionSaving(false);
    }
  }, [loadSemanticAssets, semanticRelationDefinitionEditor]);

  const saveAnalyticsModelFile = useCallback(async (contentOverride?: string) => {
    if (!analyticsModelSelectedFile) return;
    const nextContent = contentOverride ?? analyticsModelEditorContent;
    setAnalyticsModelEditorSaving(true);
    try {
      await saveFile(analyticsModelSelectedFile.path, nextContent);
      setAnalyticsModelEditorContent(nextContent);
      setAnalyticsModelEditorOriginal(nextContent);
      await loadAnalyticsModels(true);
      if (analyticsModelDetail) {
        const detail = await getAnalyticsModel(analyticsModelDetail.id);
        setAnalyticsModelDetail(detail);
        const updatedFile = (detail.files || []).find((file) => file.path === analyticsModelSelectedFile.path) || analyticsModelSelectedFile;
        setAnalyticsModelSelectedFile(updatedFile);
      }
      setActionDialog({
        type: "success",
        title: "分析模型文件已保存",
        message: "文件内容已写入磁盘，后端已刷新分析模型 registry。",
      });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setAnalyticsModelEditorSaving(false);
    }
  }, [analyticsModelDetail, analyticsModelEditorContent, analyticsModelSelectedFile, loadAnalyticsModels]);

  const handleSidebarResize = useCallback(
    (delta: number) => {
      setSidebarWidth((prev: number) => Math.max(200, prev + delta));
    },
    [setSidebarWidth]
  );

  const readyCount = useMemo(() => assets.filter((asset) => asset.profile_status === "ready").length, [assets]);
  const missingCount = assets.length - readyCount;
  const totalDataAssets = assets.length + databaseSources.length;
  const filteredSemanticAssets = useMemo(() => {
    const keyword = semanticAssetSearch.trim().toLowerCase();
    return semanticAssets.filter((asset) => {
      if (semanticAssetTypeFilter !== "all" && asset.type !== semanticAssetTypeFilter) return false;
      if (!keyword) return true;
      return asset.name.toLowerCase().includes(keyword);
    });
  }, [semanticAssetSearch, semanticAssetTypeFilter, semanticAssets]);
  const filteredAnalyticsModels = useMemo(() => {
    const keyword = analyticsModelSearch.trim().toLowerCase();
    if (!keyword) return analyticsModels;
    return analyticsModels.filter((model) => {
      const haystack = [model.name, model.id, model.description, ...(model.tags || [])].join(" ").toLowerCase();
      return haystack.includes(keyword);
    });
  }, [analyticsModelSearch, analyticsModels]);

  const generateOneProfile = useCallback(
    async (asset: TableAsset) => {
      setProfilingAssetId(asset.asset_id);
      try {
        const started = await generateTableAssetProfile(asset.asset_id);
        setToast({ type: "success", message: "Profile 生成任务已开始" });
        let job = started;
        for (let attempt = 0; attempt < 180; attempt += 1) {
          if (job.status === "succeeded") break;
          if (job.status === "failed") {
            throw new Error(job.error || "Profile 生成失败");
          }
          await new Promise((resolve) => window.setTimeout(resolve, 1000));
          job = await getTableAssetProfileJob(job.job_id);
        }
        if (job.status !== "succeeded") {
          throw new Error("Profile 生成仍在进行，请稍后刷新。");
        }
        const updated = job.asset || await getTableAsset(asset.asset_id, false);
        setAssets((current) => current.map((item) => (item.asset_id === updated.asset_id ? updated : item)));
        if (updated.source_type === "logical_concat") {
          const status = updated.profile_status;
          setToast({
            type: status === "ready" ? "success" : status === "partial" ? "warning" : "error",
            message: status === "ready"
              ? "逻辑数据集 Profile 已就绪"
              : status === "partial"
                ? "逻辑数据集摘要已更新，但仍有来源 Profile 待补充"
                : "逻辑数据集 Profile 未生成，请检查来源表",
          });
        } else {
          setToast({ type: "success", message: "Profile 已生成" });
        }
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

  const createLogicalConcatDataset = useCallback(async (payload: {
    name: string;
    description: string;
    tags: string[];
    sourceAssetIds: string[];
    schemaMode: "strict" | "baseline_fill_missing" | "union_fill_missing";
    preferredIntents: string[];
    directSourceAllowed: boolean;
  }) => {
    setConcatDatasetBusy(true);
    try {
      const asset = await createConcatDataset({ name: payload.name, description: payload.description, tags: payload.tags, source_asset_ids: payload.sourceAssetIds, schema_mode: payload.schemaMode, preferred_intents: payload.preferredIntents, direct_source_allowed: payload.directSourceAllowed });
      setAssets((current) => [asset, ...current]);
      setConcatDatasetModalOpen(false);
      setToast({ type: "success", message: `已创建逻辑数据集：${asset.file_name}` });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setConcatDatasetBusy(false);
    }
  }, []);

  const refreshLogicalConcatDataset = useCallback(async (asset: TableAsset) => {
    setRefreshingConcatDatasetId(asset.asset_id);
    try {
      const refreshed = await refreshConcatDataset(asset.asset_id);
      setAssets((current) => current.map((item) => item.asset_id === refreshed.asset_id ? refreshed : item));
      setToast({ type: "success", message: `已刷新逻辑数据集：${refreshed.file_name}` });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setRefreshingConcatDatasetId(null);
    }
  }, []);

  const saveLogicalDatasetDefinition = useCallback(async (asset: TableAsset, payload: {
    name: string;
    description: string;
    tags: string[];
    preferredIntents: string[];
    directSourceAllowed: boolean;
  }) => {
    setConcatDatasetBusy(true);
    try {
      const updated = await updateLogicalDatasetDefinition(asset.asset_id, {
        name: payload.name,
        description: payload.description,
        tags: payload.tags,
        preferred_intents: payload.preferredIntents,
        direct_source_allowed: payload.directSourceAllowed,
      });
      setAssets((current) => current.map((item) => item.asset_id === updated.asset_id ? updated : item));
      setLogicalDefinitionEditor(null);
      setLogicalDefinitionAsset((current) => current?.asset_id === updated.asset_id ? updated : current);
      setToast({ type: "success", message: `已更新逻辑数据集定义：${updated.file_name}` });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setConcatDatasetBusy(false);
    }
  }, []);

  const appendLogicalConcatSources = useCallback(async (payload: { asset: TableAsset; sourceAssetIds: string[]; schemaMode: "strict" | "baseline_fill_missing" | "union_fill_missing" }) => {
    setConcatDatasetBusy(true);
    try {
      const updated = await appendConcatDatasetSources(payload.asset.asset_id, {
        source_asset_ids: payload.sourceAssetIds,
        schema_mode: payload.schemaMode,
      });
      setAssets((current) => current.map((item) => item.asset_id === updated.asset_id ? updated : item));
      setConcatAppendTarget(null);
      setToast({ type: "success", message: `已追加 ${payload.sourceAssetIds.length} 张来源表：${updated.file_name}` });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setConcatDatasetBusy(false);
    }
  }, []);

  const removeAsset = useCallback(async (asset: TableAsset) => {
    setRemovingAssetId(asset.asset_id);
    try {
      const result = await removeTableAsset(asset.asset_id);
      setAssets((current) => current.filter((item) => !result.removed_asset_ids.includes(item.asset_id)));
      if (profileAsset && result.removed_asset_ids.includes(profileAsset.asset_id)) {
        setProfileAsset(null);
      }
      setAssetPendingRemoval(null);
      setToast({ type: "success", message: `已从智能问数移除 ${result.file_name}` });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setRemovingAssetId(null);
    }
  }, [profileAsset]);

  const removeDatabaseSource = useCallback(async (source: KnowledgeDatabaseSource) => {
    setRemovingDatabaseSourceId(source.id);
    try {
      await deleteKnowledgeDatabaseSource(source.id);
      setDatabaseSources((current) => current.filter((item) => item.id !== source.id));
      setDatabaseSourcePendingRemoval(null);
      setToast({ type: "success", message: `已移除数据库资产：${source.name}` });
    } catch (error) {
      setToast({ type: "error", message: error instanceof Error ? error.message : "移除数据库资产失败" });
    } finally {
      setRemovingDatabaseSourceId(null);
    }
  }, []);

  const openProfile = useCallback(async (asset: TableAsset) => {
    if (asset.profile_status === "missing") {
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
            <div className="workspace-page-container flex flex-col gap-5">
              <WorkspacePageHeader
                eyebrow="DATA WORKSPACE"
                title="智能问数"
                description="从数据资产开始，沉淀 Profile、语义模型与业务口径，再交给 Agent 完成可靠问数。"
                actions={
                  <Link
                    href="/knowledge"
                    className="inline-flex h-10 items-center gap-2 rounded-full bg-[#002fa7]/10 px-4 text-sm font-medium text-[#002fa7] transition hover:bg-[#002fa7]/15"
                  >
                    <Upload className="h-4 w-4" />
                    上传文件
                  </Link>
                }
              />

              {toast ? (
                <div
                  role="status"
                  className={`fixed right-6 top-6 z-[300] flex max-w-md items-start gap-2 rounded-2xl border px-4 py-3 text-sm shadow-xl ${
                    toast.type === "success"
                      ? "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                      : toast.type === "warning"
                        ? "border-amber-500/20 bg-amber-50 text-amber-800"
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
                    <p className="mt-1 text-sm text-gray-500">资产、模型、语义资产和问数入口放在同一层管理。</p>
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
                      title="语义资产"
                      description={semanticAssetsLoading ? "读取中" : `${semanticAssets.length} 个资产`}
                      onClick={() => setActiveSection("measures")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "guardrails"}
                      icon={ShieldCheck}
                      title="SQL 守卫"
                      description={`${sqlGuardrails.length} 条规则`}
                      onClick={() => setActiveSection("guardrails")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "models"}
                      icon={Layers3}
                      title="分析模型"
                      description={`${analyticsModels.length} 个模型`}
                      onClick={() => setActiveSection("models")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "tasks"}
                      icon={ListTodo}
                      title="任务中心"
                      description={taskCenterLoading ? "读取中" : `${taskCenterItems.length} 个任务`}
                      onClick={() => setActiveSection("tasks")}
                    />
                    <AnalyticsNavButton
                      active={activeSection === "results"}
                      icon={Database}
                      title="查询结果"
                      description={queryResultsLoading ? "读取中" : `${queryResults.length} 个结果`}
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

                  {activeSection === "tasks" ? (
                    <TaskCenterSection
                      tasks={taskCenterItems}
                      loading={taskCenterLoading}
                      onRefresh={loadTaskCenter}
                      onOpenTask={openTaskDetail}
                    />
                  ) : null}

                  {activeSection === "assets" ? (
                    <section className="p-5">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <h2 className="text-lg font-semibold text-gray-950">数据资产</h2>
                          <p className="mt-1 text-sm text-gray-500">
                            表格文件和数据库源统一在这里管理；Profile 是分析模型和自然语言问数理解字段的机器画像。
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={refresh}
                            disabled={loading}
                            className="inline-flex h-10 items-center gap-2 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
                          >
                            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
                            刷新数据资产
                          </button>
                          <button
                            type="button"
                            onClick={() => setConcatDatasetModalOpen(true)}
                            disabled={assets.filter((asset) => asset.source_type !== "derived_concat" && asset.source_type !== "logical_concat").length < 2}
                            className="inline-flex h-10 items-center gap-2 rounded-2xl border border-[#002fa7]/20 bg-[#002fa7]/[0.04] px-4 text-sm font-semibold text-[#002fa7] shadow-sm transition hover:bg-[#002fa7]/[0.08] disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            <Layers3 className="h-4 w-4" />
                            合并表格
                          </button>
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
                                    onRemove={(item) => setDatabaseSourcePendingRemoval(item)}
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
                                    onOpenDefinition={(item) => setLogicalDefinitionAsset(item)}
                                    onGenerateProfile={generateOneProfile}
                                    onRefreshConcat={refreshLogicalConcatDataset}
                                    onAppendConcat={(item) => setConcatAppendTarget(item)}
                                    refreshingConcat={refreshingConcatDatasetId === asset.asset_id}
                                    onRemove={(asset) => setAssetPendingRemoval(asset)}
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
                      title="分析模型"
                      subtitle="模型就是可选择的问数上下文：绑定数据资产、语义资产、守卫和输出模板，再写一份自然语言 playbook。"
                      primaryAction="新建"
                      onPrimaryAction={() => setAnalyticsModelModal("create")}
                      secondaryAction="导入"
                      secondaryIcon={Upload}
                      onSecondaryAction={() => setAnalyticsModelModal("import")}
                      tertiaryAction="刷新模型"
                      tertiaryIcon={RefreshCw}
                      tertiaryLoading={analyticsModelsLoading}
                      onTertiaryAction={handleRefreshAnalyticsModels}
                    >
                      <div className="mb-4">
                        <div className="relative">
                          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                          <input
                            value={analyticsModelSearch}
                            onChange={(event) => setAnalyticsModelSearch(event.target.value)}
                            className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white pl-9 pr-3 text-sm outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                            placeholder="搜索模型 name、id、tag"
                          />
                        </div>
                      </div>
                      {analyticsModelsLoading ? (
                        <div className="flex items-center justify-center rounded-3xl border border-dashed border-black/[0.08] py-14 text-sm text-gray-400">
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                          正在读取分析模型…
                        </div>
                      ) : analyticsModels.length === 0 ? (
                        <EmptyWorkbenchState
                          title="还没有分析模型"
                          description="可以新建一个模型包，也可以导入包含 model.md 的 ZIP 或文件夹。模型会在对话中作为强上下文注入。"
                        />
                      ) : filteredAnalyticsModels.length === 0 ? (
                        <EmptyWorkbenchState
                          title="没有匹配的分析模型"
                          description="调整搜索名称、ID 或标签后再查看。"
                        />
                      ) : (
                        <div className="grid gap-3 lg:grid-cols-2">
                          {filteredAnalyticsModels.map((model) => (
                            <AnalyticsModelCard key={model.id} model={model} onOpen={openAnalyticsModelDetail} />
                          ))}
                        </div>
                      )}
                    </WorkbenchSection>
                  ) : null}

                  {activeSection === "guardrails" ? (
                    <SqlGuardrailsSection
                      rules={sqlGuardrails}
                      types={sqlGuardrailTypes}
                      loading={sqlGuardrailsLoading}
                      busy={sqlGuardrailBusy}
                      onRefresh={loadSqlGuardrails}
                      onReset={handleResetSqlGuardrails}
                      onCreate={() => setSqlGuardrailEditor(createEmptySqlGuardrail(Object.keys(sqlGuardrailTypes)[0] || "require_group_by"))}
                      onEdit={setSqlGuardrailEditor}
                      onToggle={(rule) => handleSaveSqlGuardrail({ ...rule, enabled: !rule.enabled })}
                      onDelete={handleDeleteSqlGuardrail}
                    />
                  ) : null}

                  {activeSection === "measures" ? (
                    <WorkbenchSection
                      icon={Sigma}
                      title="语义资产"
                      subtitle="这里统一管理 Skill-like 语义资产：度量值定义业务算法，颗粒度定义统计对象，维度定义分析角度。"
                      primaryAction="新建"
                      onPrimaryAction={() => setSemanticAssetModal("create")}
                      secondaryAction="导入"
                      secondaryIcon={Upload}
                      onSecondaryAction={() => setSemanticAssetModal("import")}
                      tertiaryAction="刷新语义资产"
                      tertiaryIcon={RefreshCw}
                      tertiaryLoading={semanticAssetsLoading}
                      onTertiaryAction={handleRefreshSemanticAssets}
                    >
                      <div className="mb-4 flex flex-wrap items-center gap-3">
                        <div className="relative min-w-[260px] flex-1">
                          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
                          <input
                            value={semanticAssetSearch}
                            onChange={(event) => setSemanticAssetSearch(event.target.value)}
                            className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white pl-9 pr-3 text-sm outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                            placeholder="搜索 name"
                          />
                        </div>
                        <div className="flex shrink-0 rounded-2xl bg-gray-100 p-1">
                          {[
                            { value: "all", label: "全部" },
                            { value: "measure", label: "度量值" },
                            { value: "grain", label: "颗粒度" },
                            { value: "dimension", label: "维度" },
                            { value: "relation", label: "资产关联" },
                          ].map((item) => (
                            <button
                              key={item.value}
                              type="button"
                              onClick={() => setSemanticAssetTypeFilter(item.value as "all" | SemanticAssetType)}
                              className={`h-9 rounded-xl px-4 text-sm font-semibold transition ${
                                semanticAssetTypeFilter === item.value ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500 hover:text-gray-800"
                              }`}
                            >
                              {item.label}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div>
                        {semanticAssetsLoading ? (
                          <div className="flex items-center justify-center rounded-3xl border border-dashed border-black/[0.08] py-14 text-sm text-gray-400">
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            正在读取语义资产…
                          </div>
                        ) : semanticAssets.length === 0 ? (
                          <EmptyWorkbenchState
                            title="还没有语义资产"
                            description="可以新建度量值或维度，也可以导入包含 measure.md / dimension.md 的 ZIP 或文件夹。"
                          />
                        ) : filteredSemanticAssets.length === 0 ? (
                          <EmptyWorkbenchState
                            title="没有匹配的语义资产"
                            description="调整搜索名称或类型筛选后再查看。"
                          />
                        ) : (
                          <div className="grid gap-3 lg:grid-cols-2">
                            {filteredSemanticAssets.map((asset) => (
                              <SemanticAssetCard key={asset.id} asset={asset} onOpen={openSemanticAssetDetail} />
                            ))}
                          </div>
                        )}
                      </div>
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

      {logicalDefinitionAsset?.logical_dataset ? (
        <LogicalDatasetDefinitionModal
          asset={logicalDefinitionAsset}
          onClose={() => setLogicalDefinitionAsset(null)}
          onEdit={() => setLogicalDefinitionEditor(logicalDefinitionAsset)}
        />
      ) : null}
      {logicalDefinitionEditor?.logical_dataset ? (
        <LogicalDatasetDefinitionEditorModal
          asset={logicalDefinitionEditor}
          busy={concatDatasetBusy}
          onClose={() => setLogicalDefinitionEditor(null)}
          onSave={saveLogicalDatasetDefinition}
        />
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

      {semanticAssetModal === "create" ? (
        <SemanticAssetCreateModal
          busy={semanticAssetsBusy}
          tableAssets={assets}
          databaseSources={databaseSources}
          initialType={semanticAssetTypeFilter === "all" ? "measure" : semanticAssetTypeFilter}
          onClose={() => setSemanticAssetModal(null)}
          onCreate={handleCreateSemanticAsset}
          semanticAssets={semanticAssets}
        />
      ) : null}

      {semanticAssetModal === "import" ? (
        <SemanticAssetImportModal
          busy={semanticAssetsBusy}
          onClose={() => setSemanticAssetModal(null)}
          onImport={handleImportSemanticAssets}
        />
      ) : null}

      {analyticsModelModal === "create" ? (
        <AnalyticsModelCreateModal
          busy={analyticsModelsBusy}
          tableAssets={assets}
          databaseSources={databaseSources}
          semanticAssets={semanticAssets}
          sqlGuardrails={sqlGuardrails}
          onClose={() => setAnalyticsModelModal(null)}
          onCreate={handleCreateAnalyticsModel}
        />
      ) : null}

      {analyticsModelModal === "import" ? (
        <AnalyticsModelImportModal
          busy={analyticsModelsBusy}
          onClose={() => setAnalyticsModelModal(null)}
          onImport={handleImportAnalyticsModels}
        />
      ) : null}

      {analyticsModelDetail || analyticsModelDetailLoading ? (
        <AnalyticsModelDetailModal
          model={analyticsModelDetail}
          loading={analyticsModelDetailLoading}
          tableAssets={assets}
          databaseSources={databaseSources}
          semanticAssets={semanticAssets}
          sqlGuardrails={sqlGuardrails}
          selectedFile={analyticsModelSelectedFile}
          editorContent={analyticsModelEditorContent}
          editorOriginal={analyticsModelEditorOriginal}
          editorLoading={analyticsModelEditorLoading}
          editorSaving={analyticsModelEditorSaving}
          onClose={() => {
            setAnalyticsModelDetail(null);
            setAnalyticsModelSelectedFile(null);
            setAnalyticsModelEditorContent("");
            setAnalyticsModelEditorOriginal("");
          }}
          onSelectFile={selectAnalyticsModelFile}
          onChangeContent={setAnalyticsModelEditorContent}
          onSave={saveAnalyticsModelFile}
        />
      ) : null}

      {semanticAssetDetail || semanticAssetDetailLoading ? (
        <SemanticAssetDetailModal
          asset={semanticAssetDetail}
          loading={semanticAssetDetailLoading}
          selectedFile={semanticAssetSelectedFile}
          editorContent={semanticAssetEditorContent}
          editorOriginal={semanticAssetEditorOriginal}
          editorLoading={semanticAssetEditorLoading}
          editorSaving={semanticAssetEditorSaving}
          onClose={() => {
            setSemanticAssetDetail(null);
            setSemanticAssetSelectedFile(null);
            setSemanticAssetEditorContent("");
            setSemanticAssetEditorOriginal("");
          }}
          onSelectFile={selectSemanticAssetFile}
          onChangeContent={setSemanticAssetEditorContent}
          onSave={saveSemanticAssetFile}
          onEditDimensionDefinition={(asset) => setSemanticDimensionDefinitionEditor(asset)}
          onEditRelationDefinition={(asset) => setSemanticRelationDefinitionEditor(asset)}
        />
      ) : null}

      {semanticDimensionDefinitionEditor ? (
        <DimensionDefinitionEditorModal
          asset={semanticDimensionDefinitionEditor}
          tableAssets={assets}
          databaseSources={databaseSources}
          busy={semanticDimensionDefinitionSaving}
          initialTab={semanticDimensionDefinitionInitialTab}
          onClose={() => setSemanticDimensionDefinitionEditor(null)}
          onSave={saveSemanticDimensionDefinition}
          onSaveMarkdown={saveSemanticDimensionMarkdown}
        />
      ) : null}

      {semanticRelationDefinitionEditor ? (
        <RelationDefinitionEditorModal
          asset={semanticRelationDefinitionEditor}
          tableAssets={assets}
          databaseSources={databaseSources}
          semanticAssets={semanticAssets}
          busy={semanticDimensionDefinitionSaving}
          onClose={() => setSemanticRelationDefinitionEditor(null)}
          onSave={saveSemanticRelationDefinition}
        />
      ) : null}

      {sqlGuardrailEditor ? (
        <SqlGuardrailEditorModal
          rule={sqlGuardrailEditor}
          types={sqlGuardrailTypes}
          databaseSources={databaseSources}
          semanticAssets={semanticAssets}
          busy={sqlGuardrailBusy}
          onClose={() => setSqlGuardrailEditor(null)}
          onSave={handleSaveSqlGuardrail}
        />
      ) : null}

      {taskDetail ? (
        <TaskDetailModal
          detail={taskDetail}
          loading={taskDetailLoading}
          onClose={() => setTaskDetail(null)}
          onOpenDimensionMatching={(dimensionId) => void openTaskDimensionMatching(dimensionId)}
        />
      ) : null}

      {actionDialog ? <ActionFeedbackDialog dialog={actionDialog} onClose={() => setActionDialog(null)} /> : null}
      {concatDatasetModalOpen ? (
        <ConcatDatasetModal
          assets={assets.filter((asset) => asset.source_type !== "derived_concat" && asset.source_type !== "logical_concat")}
          busy={concatDatasetBusy}
          onClose={() => setConcatDatasetModalOpen(false)}
          onCreate={createLogicalConcatDataset}
        />
      ) : null}
      {concatAppendTarget ? (
        <AppendConcatDatasetSourcesModal
          dataset={concatAppendTarget}
          assets={assets.filter((asset) => asset.source_type !== "derived_concat" && asset.source_type !== "logical_concat")}
          busy={concatDatasetBusy}
          onClose={() => setConcatAppendTarget(null)}
          onAppend={appendLogicalConcatSources}
        />
      ) : null}
      {assetPendingRemoval ? (
        <RemoveTableAssetDialog
          asset={assetPendingRemoval}
          removing={removingAssetId === assetPendingRemoval.asset_id}
          onClose={() => setAssetPendingRemoval(null)}
          onConfirm={() => void removeAsset(assetPendingRemoval)}
        />
      ) : null}
      {databaseSourcePendingRemoval ? (
        <RemoveDatabaseSourceDialog
          source={databaseSourcePendingRemoval}
          removing={removingDatabaseSourceId === databaseSourcePendingRemoval.id}
          onClose={() => setDatabaseSourcePendingRemoval(null)}
          onConfirm={() => void removeDatabaseSource(databaseSourcePendingRemoval)}
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
        暂未发现明显实体候选。你仍然可以在分析模型 reference 里手动说明这些字段的业务含义。
      </div>
    );
  }

  return (
    <div className="mt-4 space-y-3">
      <div className="rounded-2xl border border-amber-500/15 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-800">
        这里的候选只服务于表格 Profile 和后续分析模型 reference，不会直接写入 Vanna。Vanna 只处理已配置的数据库源和数据库表。
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
  onPrimaryAction,
  secondaryAction,
  secondaryIcon: SecondaryIcon = Upload,
  onSecondaryAction,
  tertiaryAction,
  tertiaryIcon: TertiaryIcon = RefreshCw,
  tertiaryLoading = false,
  onTertiaryAction,
  children,
}: {
  icon: LucideIcon;
  title: string;
  subtitle: string;
  primaryAction: string;
  onPrimaryAction?: () => void;
  secondaryAction?: string;
  secondaryIcon?: LucideIcon;
  onSecondaryAction?: () => void;
  tertiaryAction?: string;
  tertiaryIcon?: LucideIcon;
  tertiaryLoading?: boolean;
  onTertiaryAction?: () => void;
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
        <div className="flex items-center gap-2">
          {secondaryAction ? (
            <button
              type="button"
              onClick={onSecondaryAction}
              className="inline-flex h-10 items-center gap-2 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50"
            >
              <SecondaryIcon className="h-4 w-4" />
              {secondaryAction}
            </button>
          ) : null}
          {tertiaryAction ? (
            <button
              type="button"
              onClick={onTertiaryAction}
              disabled={tertiaryLoading}
              className="inline-flex h-10 items-center gap-2 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
            >
              <TertiaryIcon className={`h-4 w-4 ${tertiaryLoading ? "animate-spin" : ""}`} />
              {tertiaryAction}
            </button>
          ) : null}
          <button
            type="button"
            onClick={onPrimaryAction}
            className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-[#001f7a]"
          >
            <PlusIcon />
            {primaryAction}
          </button>
        </div>
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

function createEmptySqlGuardrail(type: string): SqlGuardrailRule {
  return {
    id: `rule_${Date.now()}`,
    name: "新建 SQL 守卫",
    enabled: true,
    type,
    scope: {
      table_scope: {
        mode: "any",
        values: [],
      },
      semantic_assets: [],
    },
    params: {},
    action: {
      type: "rewrite",
      message: "",
    },
    document_path: `sql-guardrails/rules/rule_${Date.now()}/guardrail.md`,
    document_body: "# 新建 SQL 守卫\n\n## 业务约束\n\n在这里描述这条守卫保护的业务口径。\n\n## 禁止写法\n\n```sql\n-- 可选：写出禁止的 SQL 形态。\n```\n\n## 推荐写法\n\n```sql\n-- 可选：写出推荐 SQL 形态。\n```\n\n## 风险说明\n\n- 说明可能误伤或不能覆盖的场景。\n",
  };
}

function cloneSqlGuardrail(rule: SqlGuardrailRule): SqlGuardrailRule {
  return JSON.parse(JSON.stringify(rule)) as SqlGuardrailRule;
}

function formatScope(rule: SqlGuardrailRule): string {
  const parts = [
    rule.scope.table_scope?.values?.length
      ? `tables:${rule.scope.table_scope.mode}:${rule.scope.table_scope.values.join(",")}`
      : "",
    rule.scope.semantic_assets?.length ? `semantic:${rule.scope.semantic_assets.join(",")}` : "",
  ].filter(Boolean);
  return parts.join(" · ") || "全局";
}

function sqlGuardrailParamSummary(rule: SqlGuardrailRule): string {
  const entries = Object.entries(rule.params || {});
  if (!entries.length) return "未配置参数";
  return entries
    .slice(0, 4)
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(",") : String(value)}`)
    .join(" · ");
}

function SqlGuardrailsSection({
  rules,
  types,
  loading,
  busy,
  onRefresh,
  onReset,
  onCreate,
  onEdit,
  onToggle,
  onDelete,
}: {
  rules: SqlGuardrailRule[];
  types: Record<string, SqlGuardrailTypeDefinition>;
  loading: boolean;
  busy: boolean;
  onRefresh: () => void;
  onReset: () => void;
  onCreate: () => void;
  onEdit: (rule: SqlGuardrailRule) => void;
  onToggle: (rule: SqlGuardrailRule) => void;
  onDelete: (rule: SqlGuardrailRule) => void;
}) {
  return (
    <WorkbenchSection
      icon={ShieldCheck}
      title="SQL 守卫"
      subtitle="结构化配置 NL2SQL 的口径和性能拦截规则。规则按 type 分发到后端 detector，命中后可重写、阻断或仅告警。"
      primaryAction="新增规则"
      onPrimaryAction={onCreate}
      secondaryAction="恢复默认"
      secondaryIcon={RefreshCw}
      onSecondaryAction={onReset}
      tertiaryAction="刷新规则"
      tertiaryIcon={RefreshCw}
      tertiaryLoading={loading}
      onTertiaryAction={onRefresh}
    >
      {loading ? (
        <div className="flex items-center justify-center rounded-3xl border border-dashed border-black/[0.08] py-14 text-sm text-gray-400">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          正在读取 SQL 守卫…
        </div>
      ) : rules.length === 0 ? (
        <EmptyWorkbenchState title="还没有 SQL 守卫" description="可以新建规则，或恢复默认配置。" />
      ) : (
        <div className="space-y-3">
          {rules.map((rule) => {
            const typeDef = types[rule.type];
            return (
              <article key={rule.id} className="rounded-3xl border border-black/[0.06] bg-white p-4 shadow-sm">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${rule.enabled ? "bg-emerald-50 text-emerald-700" : "bg-gray-100 text-gray-500"}`}>
                        {rule.enabled ? "启用" : "停用"}
                      </span>
                      <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-semibold text-[#002fa7]">
                        {typeDef?.label || rule.type}
                      </span>
                      <h3 className="truncate text-sm font-semibold text-gray-950">{rule.name}</h3>
                    </div>
                    <p className="mt-2 font-mono text-xs text-gray-400">{rule.id}</p>
                    <p className="mt-2 line-clamp-2 text-sm text-gray-500">{rule.action.message || typeDef?.description || "未填写动作说明。"}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <button
                      type="button"
                      onClick={() => onToggle(rule)}
                      disabled={busy}
                      className="h-9 rounded-2xl border border-black/[0.08] px-3 text-xs font-semibold text-gray-600 transition hover:bg-gray-50 disabled:opacity-50"
                    >
                      {rule.enabled ? "停用" : "启用"}
                    </button>
                    <button
                      type="button"
                      onClick={() => onEdit(cloneSqlGuardrail(rule))}
                      className="h-9 rounded-2xl bg-[#002fa7] px-3 text-xs font-semibold text-white transition hover:bg-[#001f7a]"
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => onDelete(rule)}
                      disabled={busy}
                      className="h-9 rounded-2xl border border-red-500/20 px-3 text-xs font-semibold text-red-600 transition hover:bg-red-50 disabled:opacity-50"
                    >
                      删除
                    </button>
                  </div>
                </div>
                <div className="mt-4 grid gap-3 text-xs md:grid-cols-3">
                  <div className="rounded-2xl bg-gray-50 p-3">
                    <p className="font-semibold text-gray-500">Scope</p>
                    <p className="mt-1 break-all text-gray-600">{formatScope(rule)}</p>
                  </div>
                  <div className="rounded-2xl bg-gray-50 p-3">
                    <p className="font-semibold text-gray-500">Params</p>
                    <p className="mt-1 break-all text-gray-600">{sqlGuardrailParamSummary(rule)}</p>
                  </div>
                  <div className="rounded-2xl bg-gray-50 p-3">
                    <p className="font-semibold text-gray-500">Action</p>
                    <p className="mt-1 text-gray-600">{rule.action.type}</p>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </WorkbenchSection>
  );
}

function SemanticAssetCard({ asset, onOpen }: { asset: SemanticAssetSummary; onOpen: (asset: SemanticAssetSummary) => void }) {
  const typeLabel = asset.type === "measure" ? "度量值" : asset.type === "grain" ? "颗粒度" : asset.type === "relation" ? "资产关联" : "维度";
  return (
    <button
      type="button"
      onClick={() => onOpen(asset)}
      className="flex h-full flex-col items-stretch justify-start rounded-3xl border border-black/[0.06] bg-white p-4 text-left align-top shadow-sm transition hover:border-[#002fa7]/20 hover:bg-[#002fa7]/[0.015]"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-semibold text-[#002fa7]">
          {typeLabel}
        </span>
        {asset.type === "dimension" && asset.resolution_label ? (
          <span className="rounded-full bg-violet-50 px-2.5 py-1 text-xs font-semibold text-violet-700">
            {asset.resolution_label}
          </span>
        ) : null}
        {asset.type === "relation" && asset.relation_type ? (
          <span className="rounded-full bg-violet-50 px-2.5 py-1 text-xs font-semibold text-violet-700">
            {asset.relation_type === "dimension_binding" ? "关联维度" : "字段关联"}
          </span>
        ) : null}
        <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-gray-950" title={asset.name}>
          {asset.name}
        </h3>
      </div>
      <p className="mt-2 line-clamp-2 min-h-10 text-sm leading-5 text-gray-500">
        {asset.description || "未填写描述。"}
      </p>
      <p className="mt-3 truncate font-mono text-xs text-gray-400" title={asset.path}>
        {asset.path}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {(asset.aliases || []).slice(0, 4).map((alias) => (
          <span key={alias} className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
            {alias}
          </span>
        ))}
        {(asset.tags || []).slice(0, 4).map((tag) => (
          <span key={tag} className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
            {tag}
          </span>
        ))}
      </div>
    </button>
  );
}

function AnalyticsModelCard({ model, onOpen }: { model: AnalyticsModelSummary; onOpen: (model: AnalyticsModelSummary) => void }) {
  const dataTables = model.data_assets?.tables;
  const dataAssetCount = Array.isArray(dataTables) ? dataTables.length : 0;
  const semanticAssetCount = Object.values(model.semantic_assets || {}).reduce<number>((count, value) => {
    return count + (Array.isArray(value) ? value.length : 0);
  }, 0);

  return (
    <button
      type="button"
      onClick={() => onOpen(model)}
      className="rounded-3xl border border-black/[0.06] bg-white p-4 text-left shadow-sm transition hover:border-[#002fa7]/20 hover:bg-[#002fa7]/[0.015]"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-semibold text-[#002fa7]">
          分析模型
        </span>
        <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-gray-950" title={model.name}>
          {model.name}
        </h3>
        {model.version ? <span className="text-xs font-semibold text-gray-400">v{model.version}</span> : null}
      </div>
      <p className="mt-2 line-clamp-2 min-h-10 text-sm leading-5 text-gray-500">
        {model.description || "未填写描述。"}
      </p>
      <p className="mt-3 truncate font-mono text-xs text-gray-400" title={model.path}>
        {model.path}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
          数据资产 {dataAssetCount}
        </span>
        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-500">
          语义资产 {semanticAssetCount}
        </span>
        {(model.tags || []).slice(0, 4).map((tag) => (
          <span key={tag} className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
            {tag}
          </span>
        ))}
      </div>
    </button>
  );
}

function splitTokenList(value: string): string[] {
  return value
    .split(/[,\n，、]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function getParamValue(rule: SqlGuardrailRule, path: string): unknown {
  const key = path.replace(/^params\./, "");
  return rule.params?.[key];
}

function setParamValue(rule: SqlGuardrailRule, path: string, value: unknown): SqlGuardrailRule {
  const key = path.replace(/^params\./, "");
  return { ...rule, params: { ...(rule.params || {}), [key]: value } };
}

function SqlGuardrailEditorModal({
  rule,
  types,
  databaseSources,
  semanticAssets,
  busy,
  onClose,
  onSave,
}: {
  rule: SqlGuardrailRule;
  types: Record<string, SqlGuardrailTypeDefinition>;
  databaseSources: KnowledgeDatabaseSource[];
  semanticAssets: SemanticAssetSummary[];
  busy: boolean;
  onClose: () => void;
  onSave: (rule: SqlGuardrailRule) => void;
}) {
  const [draft, setDraft] = useState<SqlGuardrailRule>(() => cloneSqlGuardrail(rule));
  const [sourceFilter, setSourceFilter] = useState("");
  const [editMode, setEditMode] = useState<"structured" | "markdown">("structured");
  const [markdownContent, setMarkdownContent] = useState(rule.document_content || "");
  const typeDef = types[draft.type];
  const availableTypes = Object.keys(types);
  const sourceOptions = useMemo(
    () => databaseSources.map((source) => ({ value: source.name, label: source.name || source.id, hint: source.database })),
    [databaseSources]
  );
  const tableOptions = useMemo(() => {
    const seen = new Set<string>();
    const options: { value: string; label: string; hint?: string }[] = [];
    databaseSources.forEach((source) => {
      if (sourceFilter && source.name !== sourceFilter) {
        return;
      }
      (source.selected_tables || []).forEach((table) => {
        if (seen.has(table)) return;
        seen.add(table);
        options.push({ value: table, label: table, hint: source.name });
      });
    });
    return options;
  }, [databaseSources, sourceFilter]);
  const semanticAssetOptions = useMemo(
    () => semanticAssets.map((asset) => ({ value: asset.id, label: asset.id, hint: `${asset.name} · ${asset.type}` })),
    [semanticAssets]
  );

  const updateSemanticAssets = (value: string[]) => {
    setDraft((current) => ({
      ...current,
      scope: {
        ...current.scope,
        semantic_assets: value,
      },
    }));
  };

  const updateAction = (updates: Partial<SqlGuardrailAction>) => {
    setDraft((current) => ({ ...current, action: { ...current.action, ...updates } }));
  };

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-950">编辑 SQL 守卫</h3>
            <p className="mt-1 text-sm text-gray-500">SQL 守卫以 guardrail.md 保存；frontmatter 编译为后端 detector，正文用于审核和 LLM 理解。</p>
            <p className="mt-2 font-mono text-xs text-gray-400">{draft.document_path || `sql-guardrails/rules/${draft.id}/guardrail.md`}</p>
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

        <div className="grid flex-1 min-h-0 gap-5 overflow-y-auto p-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          <div className="space-y-5">
            <div className="inline-grid grid-cols-2 rounded-2xl bg-gray-100 p-1">
              {[
                { value: "structured", label: "结构化录入" },
                { value: "markdown", label: "原始 Markdown" },
              ].map((item) => (
                <button
                  key={item.value}
                  type="button"
                  onClick={() => setEditMode(item.value as "structured" | "markdown")}
                  className={`h-10 rounded-xl px-4 text-sm font-semibold transition ${
                    editMode === item.value ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500 hover:text-gray-800"
                  }`}
                >
                  {item.label}
                </button>
              ))}
            </div>

            {editMode === "structured" ? (
              <>
            <section className="rounded-3xl border border-black/[0.06] p-4">
              <h4 className="text-sm font-semibold text-gray-950">基础信息</h4>
              <div className="mt-4 grid items-start gap-4 md:grid-cols-2">
                <LabeledInput
                  label="ID"
                  value={draft.id}
                  onChange={(value) => setDraft((current) => ({ ...current, id: value.trim() }))}
                  placeholder="config_rate_model_key_group"
                />
                <LabeledInput
                  label="名称"
                  value={draft.name}
                  onChange={(value) => setDraft((current) => ({ ...current, name: value }))}
                  placeholder="配置率款型颗粒度分组"
                />
                <label className="block min-h-[86px]">
                  <span className="text-xs font-semibold text-gray-500">类型</span>
                  <select
                    value={draft.type}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        type: event.target.value,
                        params: {},
                      }))
                    }
                    className="mt-1 h-12 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                  >
                    {(availableTypes.length ? availableTypes : [draft.type]).map((type) => (
                      <option key={type} value={type}>
                        {types[type]?.label || type}
                      </option>
                    ))}
                  </select>
                  <span className="mt-1 block text-xs text-gray-400">规则命中 SQL 后触发，可配置例外片段。</span>
                </label>
                <label className="block min-h-[86px]">
                  <span className="text-xs font-semibold text-gray-500">启用</span>
                  <span className="mt-1 flex h-12 items-center justify-between rounded-2xl border border-black/[0.08] bg-white px-3">
                    <span className="text-sm font-medium text-gray-700">{draft.enabled ? "已启用" : "已停用"}</span>
                    <input
                      type="checkbox"
                      checked={draft.enabled}
                      onChange={(event) => setDraft((current) => ({ ...current, enabled: event.target.checked }))}
                      className="h-5 w-5 accent-[#002fa7]"
                    />
                  </span>
                  <span className="mt-1 block text-xs text-gray-400">关闭后只保存，不参与检测。</span>
                </label>
              </div>
              {typeDef ? <p className="mt-3 text-xs leading-5 text-gray-500">{typeDef.description}</p> : null}
            </section>

            <section className="rounded-3xl border border-black/[0.06] p-4">
              <h4 className="text-sm font-semibold text-gray-950">Scope</h4>
              <p className="mt-1 text-xs text-gray-500">规则只按路由表和语义资产命中；表范围里的数据源下拉只帮助筛表，不会写入规则。</p>
              <div className="mt-4 grid gap-4 md:grid-cols-2">
                <div className="min-h-[170px] rounded-2xl border border-black/[0.06] p-3">
                  <span className="text-xs font-semibold text-gray-500">表匹配方式</span>
                  <div className="mt-2 grid grid-cols-2 gap-2 rounded-2xl bg-gray-100 p-1">
                    {[
                      { value: "any", label: "任意命中" },
                      { value: "all", label: "全部命中" },
                    ].map((item) => (
                      <button
                        key={item.value}
                        type="button"
                        onClick={() =>
                          setDraft((current) => ({
                            ...current,
                            scope: {
                              ...current.scope,
                              table_scope: {
                                ...(current.scope.table_scope || { values: [] }),
                                mode: item.value as "any" | "all",
                              },
                            },
                          }))
                        }
                        className={`h-10 rounded-xl text-sm font-semibold transition ${
                          draft.scope.table_scope?.mode === item.value ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500 hover:text-gray-800"
                        }`}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                  <p className="mt-3 text-xs leading-5 text-gray-500">
                    任意命中表示路由包含任一表即生效；全部命中表示路由必须同时包含所有表。
                  </p>
                </div>
                <MultiSelectField
                  className="md:col-span-2"
                  label="表范围 table_scope.values"
                  values={draft.scope.table_scope?.values || []}
                  options={tableOptions}
                  emptyText={sourceFilter ? "所选数据源没有已选表" : "暂无已选表"}
                  filter={{
                    label: "筛表",
                    value: sourceFilter,
                    options: sourceOptions,
                    placeholder: "全部数据源",
                    onChange: setSourceFilter,
                  }}
                  onChange={(values) =>
                    setDraft((current) => ({
                      ...current,
                      scope: {
                        ...current.scope,
                        table_scope: {
                          ...(current.scope.table_scope || { mode: "any" }),
                          values,
                        },
                      },
                    }))
                  }
                />
                <MultiSelectField
                  className="md:col-span-2"
                  label="语义资产 semantic_assets"
                  values={draft.scope.semantic_assets || []}
                  options={semanticAssetOptions}
                  emptyText="暂无语义资产"
                  onChange={updateSemanticAssets}
                />
              </div>
            </section>

            <section className="rounded-3xl border border-black/[0.06] p-4">
              <h4 className="text-sm font-semibold text-gray-950">Params</h4>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                {(typeDef?.fields || []).map((field) => {
                  const value = getParamValue(draft, field.path);
                  if (field.type === "string_array") {
                    return (
                      <LabeledTextarea
                        key={field.path}
                        label={`${field.label}${field.required ? " *" : ""}`}
                        value={Array.isArray(value) ? value.join(", ") : String(value || "")}
                        onChange={(nextValue) => setDraft((current) => setParamValue(current, field.path, splitTokenList(nextValue)))}
                      />
                    );
                  }
                  if (field.type === "number") {
                    return (
                      <LabeledInput
                        key={field.path}
                        label={`${field.label}${field.required ? " *" : ""}`}
                        value={String(value ?? "")}
                        onChange={(nextValue) =>
                          setDraft((current) => setParamValue(current, field.path, Number.parseInt(nextValue, 10) || 0))
                        }
                        inputType="number"
                      />
                    );
                  }
                  return (
                    <LabeledInput
                      key={field.path}
                      label={`${field.label}${field.required ? " *" : ""}`}
                      value={String(value ?? "")}
                      onChange={(nextValue) => setDraft((current) => setParamValue(current, field.path, nextValue))}
                    />
                  );
                })}
                {!typeDef?.fields?.length ? (
                  <p className="text-sm text-gray-400">这个规则类型没有声明参数字段。</p>
                ) : null}
              </div>
            </section>

            <section className="rounded-3xl border border-black/[0.06] p-4">
              <h4 className="text-sm font-semibold text-gray-950">Action</h4>
              <div className="mt-4 grid gap-3 md:grid-cols-[180px_minmax(0,1fr)]">
                <label className="block">
                  <span className="text-xs font-semibold text-gray-500">动作</span>
                  <select
                    value={draft.action.type}
                    onChange={(event) => updateAction({ type: event.target.value as SqlGuardrailActionType })}
                    className="mt-1 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                  >
                    <option value="rewrite">rewrite</option>
                    <option value="block">block</option>
                    <option value="warn">warn</option>
                  </select>
                </label>
                <LabeledTextarea
                  label="提示信息"
                  value={draft.action.message || ""}
                  onChange={(value) => updateAction({ message: value })}
                  placeholder="命中后写入重写 prompt 或错误信息。"
                />
              </div>
            </section>
            <section className="rounded-3xl border border-black/[0.06] p-4">
              <h4 className="text-sm font-semibold text-gray-950">文档说明</h4>
              <p className="mt-1 text-xs text-gray-500">保存到 guardrail.md 的正文。这里给用户和 LLM 看，不直接作为 detector 参数。</p>
              <textarea
                value={draft.document_body || ""}
                onChange={(event) => setDraft((current) => ({ ...current, document_body: event.target.value }))}
                rows={14}
                className="mt-4 w-full resize-y rounded-2xl border border-black/[0.08] bg-white px-3 py-3 font-mono text-xs leading-5 outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                placeholder="# 守卫说明..."
              />
            </section>
              </>
            ) : (
              <section className="rounded-3xl border border-black/[0.06] p-4">
                <h4 className="text-sm font-semibold text-gray-950">原始 guardrail.md</h4>
                <p className="mt-1 text-xs text-gray-500">直接编辑完整 Markdown。保存时后端会重新解析 frontmatter 并编译为 GuardrailRule。</p>
                <textarea
                  value={markdownContent}
                  onChange={(event) => setMarkdownContent(event.target.value)}
                  rows={32}
                  className="mt-4 w-full resize-y rounded-2xl border border-black/[0.08] bg-white px-3 py-3 font-mono text-xs leading-5 outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
              </section>
            )}
          </div>

          <aside className="min-w-0">
            <div className="sticky top-0 rounded-3xl bg-gray-950 p-4 text-gray-100">
              <p className="text-sm font-semibold">GuardrailRule 预览</p>
              <pre className="mt-3 max-h-[62vh] overflow-auto text-xs leading-5 text-gray-200">
                {JSON.stringify(draft, null, 2)}
              </pre>
            </div>
          </aside>
        </div>

        <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4">
          <button
            type="button"
            onClick={onClose}
            className="h-10 rounded-2xl border border-black/[0.08] px-4 text-sm font-semibold text-gray-700 transition hover:bg-gray-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => onSave(editMode === "markdown" ? { ...draft, document_content: markdownContent } : draft)}
            disabled={busy || (editMode === "structured" && (!draft.id.trim() || !draft.name.trim())) || (editMode === "markdown" && !markdownContent.trim())}
            className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            保存
          </button>
        </div>
      </div>
    </div>
  );
}

function LabeledInput({
  label,
  value,
  onChange,
  placeholder = "",
  inputType = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  inputType?: string;
}) {
  return (
    <label className="block">
      <span className="text-xs font-semibold text-gray-500">{label}</span>
      <input
        type={inputType}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="mt-1 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
      />
    </label>
  );
}

function LabeledTextarea({
  label,
  value,
  onChange,
  placeholder = "",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="text-xs font-semibold text-gray-500">{label}</span>
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        rows={3}
        className="mt-1 w-full resize-y rounded-2xl border border-black/[0.08] bg-white px-3 py-2 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
      />
    </label>
  );
}

function MultiSelectField({
  label,
  values,
  options,
  emptyText,
  onChange,
  className = "",
  filter,
}: {
  label: string;
  values: string[];
  options: { value: string; label: string; hint?: string }[];
  emptyText: string;
  onChange: (values: string[]) => void;
  className?: string;
  filter?: {
    label: string;
    value: string;
    options: { value: string; label: string; hint?: string }[];
    placeholder: string;
    onChange: (value: string) => void;
  };
}) {
  const normalizedValues = values.filter(Boolean);
  const optionValues = new Set(options.map((option) => option.value));
  const missingValues = normalizedValues.filter((value) => !optionValues.has(value));
  const visibleOptions = [
    ...options,
    ...missingValues.map((value) => ({ value, label: value, hint: "当前配置" })),
  ];

  const toggle = (value: string) => {
    if (normalizedValues.includes(value)) {
      onChange(normalizedValues.filter((item) => item !== value));
    } else {
      onChange([...normalizedValues, value]);
    }
  };

  return (
    <div className={`min-h-[170px] rounded-2xl border border-black/[0.06] p-3 ${className}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold text-gray-500">{label}</span>
        <div className="flex items-center gap-2">
          {filter ? (
            <label className="flex items-center gap-1 text-[11px] font-semibold text-gray-400">
              <span>{filter.label}</span>
              <select
                value={filter.value}
                onChange={(event) => filter.onChange(event.target.value)}
                className="h-7 rounded-full border border-black/[0.08] bg-white pl-2 pr-7 text-[11px] font-semibold text-gray-500 outline-none focus:border-[#002fa7]/40 focus:ring-2 focus:ring-[#002fa7]/[0.08]"
              >
                <option value="">{filter.placeholder}</option>
                {filter.options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                    {option.hint ? ` · ${option.hint}` : ""}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {normalizedValues.length ? (
            <button
              type="button"
              onClick={() => onChange([])}
              className="text-xs font-semibold text-gray-400 transition hover:text-red-600"
            >
              清空
            </button>
          ) : null}
        </div>
      </div>
      <div className="mt-2 flex min-h-7 flex-wrap gap-1.5">
        {normalizedValues.length ? (
          normalizedValues.map((value) => (
            <span key={value} className="inline-flex max-w-full items-center gap-1 rounded-full bg-[#002fa7]/10 px-2 py-1 text-[11px] font-semibold text-[#002fa7]">
              <span className="truncate">{value}</span>
              <button type="button" onClick={() => toggle(value)} className="text-[#002fa7]/70 hover:text-[#002fa7]" aria-label={`移除 ${value}`}>
                ×
              </button>
            </span>
          ))
        ) : (
          <span className="text-xs text-gray-400">不限制</span>
        )}
      </div>
      <div className="mt-3 max-h-40 space-y-1 overflow-y-auto rounded-xl bg-gray-50 p-2">
        {visibleOptions.length ? (
          visibleOptions.map((option) => {
            const checked = normalizedValues.includes(option.value);
            return (
              <label
                key={option.value}
                className={`flex cursor-pointer items-center justify-between gap-2 rounded-xl px-2 py-2 text-xs transition ${
                  checked ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-600 hover:bg-white"
                }`}
              >
                <span className="min-w-0">
                  <span className="block truncate font-semibold">{option.label}</span>
                  {option.hint ? <span className="mt-0.5 block truncate text-[11px] text-gray-400">{option.hint}</span> : null}
                </span>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(option.value)}
                  className="h-4 w-4 shrink-0 accent-[#002fa7]"
                />
              </label>
            );
          })
        ) : (
          <p className="px-2 py-4 text-center text-xs text-gray-400">{emptyText}</p>
        )}
      </div>
    </div>
  );
}

function ModelOptionPicker({
  title,
  emptyText,
  options,
  values,
  onToggle,
  onToggleAll,
}: {
  title: string;
  emptyText: string;
  options: Array<{ value: string; label: string; hint?: string }>;
  values: string[];
  onToggle: (value: string) => void;
  onToggleAll: (nextValues: string[]) => void;
}) {
  const allSelected = options.length > 0 && options.every((option) => values.includes(option.value));
  return (
    <section className="rounded-3xl border border-black/[0.06] p-4">
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-sm font-semibold text-gray-950">{title}</h4>
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-gray-400">{values.length} 已选</span>
          <button
            type="button"
            disabled={options.length === 0}
            onClick={() => onToggleAll(allSelected ? [] : options.map((option) => option.value))}
            className="h-7 rounded-lg border border-[#002fa7]/20 px-2 text-[11px] font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/[0.04] disabled:cursor-not-allowed disabled:opacity-40"
          >
            {allSelected ? "全不选" : "全选"}
          </button>
        </div>
      </div>
      <div className="mt-3 max-h-44 space-y-1 overflow-y-auto rounded-2xl bg-gray-50 p-2">
        {options.length ? (
          options.map((option) => {
            const checked = values.includes(option.value);
            return (
              <label
                key={option.value}
                className={`flex cursor-pointer items-center justify-between gap-2 rounded-xl px-3 py-2 text-xs transition ${
                  checked ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-600 hover:bg-white"
                }`}
              >
                <span className="min-w-0">
                  <span className="block truncate font-semibold">{option.label}</span>
                  {option.hint ? <span className="mt-0.5 block truncate text-[11px] text-gray-400">{option.hint}</span> : null}
                </span>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => onToggle(option.value)}
                  className="h-4 w-4 shrink-0 accent-[#002fa7]"
                />
              </label>
            );
          })
        ) : (
          <p className="px-2 py-6 text-center text-xs text-gray-400">{emptyText}</p>
        )}
      </div>
      {values.length ? (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {values.slice(0, 6).map((value) => (
            <span key={value} className="max-w-full truncate rounded-full bg-[#002fa7]/10 px-2 py-1 text-[11px] font-semibold text-[#002fa7]">
              {value}
            </span>
          ))}
          {values.length > 6 ? <span className="rounded-full bg-gray-100 px-2 py-1 text-[11px] text-gray-500">+{values.length - 6}</span> : null}
        </div>
      ) : null}
    </section>
  );
}

function AnalyticsModelCreateModal({
  busy,
  tableAssets,
  databaseSources,
  semanticAssets,
  sqlGuardrails,
  onClose,
  onCreate,
}: {
  busy: boolean;
  tableAssets: TableAsset[];
  databaseSources: KnowledgeDatabaseSource[];
  semanticAssets: SemanticAssetSummary[];
  sqlGuardrails: SqlGuardrailRule[];
  onClose: () => void;
  onCreate: (payload: {
    name: string;
    description: string;
    tags: string[];
    version: string;
    data_assets: Record<string, unknown>;
    semantic_assets: Record<string, unknown>;
    guardrails: string[];
    asset_relations?: string[];
    templates: Record<string, unknown>;
    default_template: string | null;
  }) => void;
}) {
  const [name, setName] = useState("");
  const [version, setVersion] = useState("0.1.0");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [selectedTables, setSelectedTables] = useState<string[]>([]);
  const [selectedMeasures, setSelectedMeasures] = useState<string[]>([]);
  const [selectedDimensions, setSelectedDimensions] = useState<string[]>([]);
  const [selectedGrains, setSelectedGrains] = useState<string[]>([]);
  const [selectedRelations, setSelectedRelations] = useState<string[]>([]);
  const [selectedGuardrails, setSelectedGuardrails] = useState<string[]>([]);
  const [defaultTemplate, setDefaultTemplate] = useState("");

  const tableOptions = useMemo(() => {
    const options: Array<{ value: string; label: string; hint: string }> = [];
    tableAssets.forEach((asset) => {
      options.push({
        value: `table_asset:${asset.asset_id}`,
        label: asset.file_name,
        hint: sourceTypeLabel(asset),
      });
    });
    databaseSources.forEach((source) => {
      (source.selected_tables || []).forEach((table) => {
        options.push({
          value: `${source.id}.${table}`,
          label: table,
          hint: source.name || source.id,
        });
      });
    });
    const seen = new Set<string>();
    return options.filter((option) => {
      if (seen.has(option.value)) return false;
      seen.add(option.value);
      return true;
    });
  }, [databaseSources, tableAssets]);

  const measures = semanticAssets.filter((asset) => asset.type === "measure");
  const dimensions = semanticAssets.filter((asset) => asset.type === "dimension");
  const grains = semanticAssets.filter((asset) => asset.type === "grain");
  const relationAssets = semanticAssets.filter((asset) => asset.type === "relation");
  const availableRelations = relationAssets.filter((asset) => {
    const relation = asset.relation_definition;
    if (!relation) return false;
    if (asset.relation_type === "dimension_binding") {
      const assetRef = relation.asset?.ref;
      const dimensionRef = relation.dimension?.ref;
      return Boolean(selectedTables.length > 1 && assetRef && dimensionRef && selectedTables.includes(assetRef));
    }
    if (asset.relation_type === "direct_join") {
      const leftRef = relation.left?.ref;
      const rightRef = relation.right?.ref;
      return Boolean(leftRef && rightRef && selectedTables.includes(leftRef) && selectedTables.includes(rightRef));
    }
    return false;
  });
  useEffect(() => {
    const valid = new Set(availableRelations.map((asset) => asset.id));
    setSelectedRelations((current) => current.filter((relationId) => valid.has(relationId)));
  }, [selectedTables, semanticAssets]);
  const toggle = (values: string[], value: string, setter: (next: string[]) => void) => {
    setter(values.includes(value) ? values.filter((item) => item !== value) : [...values, value]);
  };
  const toggleDimension = (dimensionId: string) => {
    if (!selectedDimensions.includes(dimensionId)) {
      setSelectedDimensions([...selectedDimensions, dimensionId]);
      return;
    }
    setSelectedDimensions(selectedDimensions.filter((item) => item !== dimensionId));
    const boundRelationIds = new Set(
      relationAssets
        .filter((asset) => asset.relation_type === "dimension_binding" && asset.relation_definition?.dimension?.ref === dimensionId)
        .map((asset) => asset.id)
    );
    setSelectedRelations((current) => current.filter((relationId) => !boundRelationIds.has(relationId)));
  };
  const toggleRelation = (relationId: string) => {
    if (selectedRelations.includes(relationId)) {
      setSelectedRelations(selectedRelations.filter((item) => item !== relationId));
      return;
    }
    const relation = relationAssets.find((asset) => asset.id === relationId);
    const dimensionRef = relation?.relation_type === "dimension_binding" ? relation.relation_definition?.dimension?.ref : undefined;
    if (dimensionRef && !selectedDimensions.includes(dimensionRef)) {
      setSelectedDimensions([...selectedDimensions, dimensionRef]);
    }
    setSelectedRelations([...selectedRelations, relationId]);
  };
  const setAllDimensions = (nextDimensions: string[]) => {
    const selected = new Set(nextDimensions);
    setSelectedDimensions(nextDimensions);
    setSelectedRelations((current) => current.filter((relationId) => {
      const relation = relationAssets.find((asset) => asset.id === relationId);
      const dimensionRef = relation?.relation_type === "dimension_binding" ? relation.relation_definition?.dimension?.ref : undefined;
      return !dimensionRef || selected.has(dimensionRef);
    }));
  };
  const setAllRelations = (nextRelations: string[]) => {
    setSelectedRelations(nextRelations);
    const requiredDimensions = relationAssets
      .filter((asset) => nextRelations.includes(asset.id) && asset.relation_type === "dimension_binding")
      .map((asset) => asset.relation_definition?.dimension?.ref)
      .filter((value): value is string => Boolean(value));
    if (requiredDimensions.length) {
      setSelectedDimensions((current) => Array.from(new Set([...current, ...requiredDimensions])));
    }
  };

  const templatePreview = `---
formatter: analytics-model
id: ${name ? name.trim().toLowerCase().replace(/\s+/g, "_") : "product_config_analysis"}
name: ${name || "产品配置分析"}
version: ${version || "0.1.0"}
description: ${description || "描述这个模型解决的业务分析问题"}
tags: ${JSON.stringify(splitTokenList(tags))}
data_assets:
  tables: ${JSON.stringify(selectedTables)}
semantic_assets:
  measures: ${JSON.stringify(selectedMeasures)}
  dimensions: ${JSON.stringify(selectedDimensions)}
  grains: ${JSON.stringify(selectedGrains)}
guardrails: ${JSON.stringify(selectedGuardrails)}
asset_relations: ${JSON.stringify(selectedRelations)}
templates: {}
default_template: ${JSON.stringify(defaultTemplate.trim() || null)}
created: YYYY-MM-DD HH:mm:ss
updated_at: YYYY-MM-DD HH:mm:ss
---`;

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="flex max-h-[88vh] w-full max-w-6xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-950">新建分析模型</h3>
            <p className="mt-1 text-sm text-gray-500">生成 model.md，后续可编辑数据资产、语义资产、模板和 playbook。</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="grid min-h-0 flex-1 gap-5 overflow-y-auto px-6 py-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_340px]">
          <div className="space-y-4">
            <div className="rounded-3xl border border-black/[0.06] p-4">
              <h4 className="text-sm font-semibold text-gray-950">基础信息</h4>
              <div className="mt-3 space-y-3">
                <label className="block text-sm font-semibold text-gray-700">
                  名称
                  <input
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    placeholder="产品配置分析"
                  />
                </label>
                <label className="block text-sm font-semibold text-gray-700">
                  版本
                  <input
                    value={version}
                    onChange={(event) => setVersion(event.target.value)}
                    className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    placeholder="0.1.0"
                  />
                </label>
                <label className="block text-sm font-semibold text-gray-700">
                  描述
                  <textarea
                    value={description}
                    onChange={(event) => setDescription(event.target.value)}
                    className="mt-2 min-h-24 w-full rounded-2xl border border-black/[0.08] bg-white px-3 py-2 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    placeholder="说明适用问题、默认分析路径、输出要求。"
                  />
                </label>
                <label className="block text-sm font-semibold text-gray-700">
                  标签
                  <input
                    value={tags}
                    onChange={(event) => setTags(event.target.value)}
                    className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    placeholder="多个标签用逗号分隔"
                  />
                </label>
                <label className="block text-sm font-semibold text-gray-700">
                  默认模板
                  <input
                    value={defaultTemplate}
                    onChange={(event) => setDefaultTemplate(event.target.value)}
                    className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    placeholder="例如 templates/report.md"
                  />
                </label>
              </div>
            </div>
            <ModelOptionPicker
              title="数据资产"
              emptyText="暂无可选表。先在数据资产中接入数据库或导入表格。"
              options={tableOptions}
              values={selectedTables}
              onToggle={(value) => toggle(selectedTables, value, setSelectedTables)}
              onToggleAll={setSelectedTables}
            />
          </div>
          <div className="space-y-4">
            <ModelOptionPicker
              title="度量值"
              emptyText="暂无度量值。"
              options={measures.map((asset) => ({ value: asset.id, label: asset.name, hint: asset.id }))}
              values={selectedMeasures}
              onToggle={(value) => toggle(selectedMeasures, value, setSelectedMeasures)}
              onToggleAll={setSelectedMeasures}
            />
            <ModelOptionPicker
              title="维度"
              emptyText="暂无维度。"
              options={dimensions.map((asset) => ({ value: asset.id, label: asset.name, hint: asset.id }))}
              values={selectedDimensions}
              onToggle={toggleDimension}
              onToggleAll={setAllDimensions}
            />
            <ModelOptionPicker
              title="颗粒度"
              emptyText="暂无颗粒度。"
              options={grains.map((asset) => ({ value: asset.id, label: asset.name, hint: asset.id }))}
              values={selectedGrains}
              onToggle={(value) => toggle(selectedGrains, value, setSelectedGrains)}
              onToggleAll={setSelectedGrains}
            />
            <ModelOptionPicker
              title="资产关联"
              emptyText={selectedTables.length > 1 ? "所选数据资产之间没有可用的已发布关联。" : "选择至少两个数据资产后显示可用关联。"}
              options={availableRelations.map((asset) => ({ value: asset.id, label: asset.name, hint: asset.relation_type === "dimension_binding" ? "关联维度" : "字段关联" }))}
              values={selectedRelations}
              onToggle={toggleRelation}
              onToggleAll={setAllRelations}
            />
            <ModelOptionPicker
              title="SQL 守卫"
              emptyText="暂无 SQL 守卫。"
              options={sqlGuardrails.map((rule) => ({ value: rule.id, label: rule.name, hint: rule.type }))}
              values={selectedGuardrails}
              onToggle={(value) => toggle(selectedGuardrails, value, setSelectedGuardrails)}
              onToggleAll={setSelectedGuardrails}
            />
          </div>
          <div className="rounded-3xl bg-gray-950 p-4 text-xs text-gray-100">
            <p className="mb-3 font-semibold text-white">YAML 预览</p>
            <pre className="max-h-[620px] overflow-auto whitespace-pre-wrap leading-5">{templatePreview}</pre>
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4">
          <button type="button" onClick={onClose} className="h-10 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700">
            取消
          </button>
          <button
            type="button"
            disabled={busy || !name.trim()}
            onClick={() =>
              onCreate({
                name,
                description,
                tags: splitTokenList(tags),
                version: version.trim() || "0.1.0",
                data_assets: { tables: selectedTables },
                semantic_assets: {
                  measures: selectedMeasures,
                  dimensions: selectedDimensions,
                  grains: selectedGrains,
                },
                guardrails: selectedGuardrails,
                asset_relations: selectedRelations,
                templates: {},
                default_template: defaultTemplate.trim() || null,
              })
            }
            className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            创建
          </button>
        </div>
      </div>
    </div>
  );
}

function AnalyticsModelImportModal({
  busy,
  onClose,
  onImport,
}: {
  busy: boolean;
  onClose: () => void;
  onImport: (files: File[]) => void;
}) {
  const [mode, setMode] = useState<"zip" | "folder">("zip");
  const [files, setFiles] = useState<File[]>([]);

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="w-full max-w-xl overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-950">导入分析模型</h3>
            <p className="mt-1 text-sm text-gray-500">支持 ZIP 或文件夹，至少包含一个 model.md。</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="space-y-4 px-6 py-5">
          <div className="inline-flex rounded-2xl bg-gray-100 p-1">
            {(["zip", "folder"] as const).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => {
                  setMode(item);
                  setFiles([]);
                }}
                className={`h-9 rounded-xl px-4 text-sm font-semibold transition ${
                  mode === item ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500"
                }`}
              >
                {item === "zip" ? "ZIP" : "文件夹"}
              </button>
            ))}
          </div>
          <label className="flex min-h-40 cursor-pointer flex-col items-center justify-center rounded-3xl border border-dashed border-black/[0.12] bg-[#f8fafc] px-5 py-8 text-center transition hover:border-[#002fa7]/30 hover:bg-[#002fa7]/[0.025]">
            <Upload className="h-8 w-8 text-[#002fa7]" />
            <span className="mt-3 text-sm font-semibold text-gray-800">
              {files.length ? `已选择 ${files.length} 个文件` : mode === "zip" ? "选择 ZIP 文件" : "选择分析模型文件夹"}
            </span>
            <span className="mt-1 text-xs text-gray-400">后端会归一化到 backend/analytics-models</span>
            <input
              type="file"
              className="hidden"
              accept={mode === "zip" ? ".zip" : undefined}
              multiple={mode === "folder"}
              {...(mode === "folder" ? { webkitdirectory: "" } : {})}
              onChange={(event) => setFiles(Array.from(event.target.files || []))}
            />
          </label>
          {files.length ? (
            <div className="max-h-28 overflow-auto rounded-2xl bg-gray-50 p-3 text-xs text-gray-500">
              {files.slice(0, 8).map((file) => {
                const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
                return <p key={`${file.name}-${file.size}`} className="truncate">{relativePath || file.name}</p>;
              })}
              {files.length > 8 ? <p>还有 {files.length - 8} 个文件…</p> : null}
            </div>
          ) : null}
        </div>
        <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4">
          <button type="button" onClick={onClose} className="h-10 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700">
            取消
          </button>
          <button
            type="button"
            disabled={busy || files.length === 0}
            onClick={() => onImport(files)}
            className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            导入
          </button>
        </div>
      </div>
    </div>
  );
}

function analyticsModelBodyFromContent(content: string, fallback = ""): string {
  if (!content.startsWith("---")) return content || fallback;
  const match = content.match(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/);
  return match ? content.slice(match[0].length).replace(/^\r?\n/, "") : fallback;
}

function localTimestamp(): string {
  const date = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function buildAnalyticsModelContent(
  model: AnalyticsModelDetail,
  draft: {
    name: string;
    version: string;
    description: string;
    tags: string[];
    tables: string[];
    measures: string[];
    dimensions: string[];
    grains: string[];
    assetRelations: string[];
    guardrails: string[];
    defaultTemplate: string;
  },
  currentContent: string
): string {
  const frontmatter = model.frontmatter || {};
  const id = String(frontmatter.id || model.id);
  const created = String(frontmatter.created || localTimestamp());
  const body = analyticsModelBodyFromContent(currentContent, model.body || "");
  const currentDataAssets = frontmatter.data_assets && typeof frontmatter.data_assets === "object"
    ? frontmatter.data_assets as Record<string, unknown>
    : {};
  const currentSemanticAssets = frontmatter.semantic_assets && typeof frontmatter.semantic_assets === "object"
    ? frontmatter.semantic_assets as Record<string, unknown>
    : {};
  const nextFrontmatter: Record<string, unknown> = {
    ...frontmatter,
    formatter: frontmatter.formatter || "analytics-model",
    id,
    name: draft.name || model.name,
    type: frontmatter.type || "analysis_model",
    version: draft.version || "0.1.0",
    description: draft.description,
    tags: draft.tags,
    data_assets: { ...currentDataAssets, tables: draft.tables },
    semantic_assets: {
      ...currentSemanticAssets,
      measures: draft.measures,
      dimensions: draft.dimensions,
      grains: draft.grains,
    },
    asset_relations: draft.assetRelations,
    guardrails: draft.guardrails,
    templates: frontmatter.templates ?? model.templates ?? {},
    default_template: draft.defaultTemplate.trim() || null,
    created,
    updated_at: localTimestamp(),
  };
  return `---\n${JSON.stringify(nextFrontmatter, null, 2)}\n---\n\n${body}`;
}

function AnalyticsModelDetailModal({
  model,
  loading,
  tableAssets,
  databaseSources,
  semanticAssets,
  sqlGuardrails,
  selectedFile,
  editorContent,
  editorOriginal,
  editorLoading,
  editorSaving,
  onClose,
  onSelectFile,
  onChangeContent,
  onSave,
}: {
  model: AnalyticsModelDetail | null;
  loading: boolean;
  tableAssets: TableAsset[];
  databaseSources: KnowledgeDatabaseSource[];
  semanticAssets: SemanticAssetSummary[];
  sqlGuardrails: SqlGuardrailRule[];
  selectedFile: AnalyticsModelFile | null;
  editorContent: string;
  editorOriginal: string;
  editorLoading: boolean;
  editorSaving: boolean;
  onClose: () => void;
  onSelectFile: (file: AnalyticsModelFile) => void;
  onChangeContent: (value: string) => void;
  onSave: (contentOverride?: string) => void;
}) {
  const files = model?.files || [];
  const fileDirty = editorContent !== editorOriginal;
  const [mode, setMode] = useState<"config" | "file" | "export">("config");
  const [draftName, setDraftName] = useState("");
  const [draftVersion, setDraftVersion] = useState("0.1.0");
  const [draftDescription, setDraftDescription] = useState("");
  const [draftTags, setDraftTags] = useState("");
  const [draftTables, setDraftTables] = useState<string[]>([]);
  const [draftMeasures, setDraftMeasures] = useState<string[]>([]);
  const [draftDimensions, setDraftDimensions] = useState<string[]>([]);
  const [draftGrains, setDraftGrains] = useState<string[]>([]);
  const [draftRelations, setDraftRelations] = useState<string[]>([]);
  const [draftGuardrails, setDraftGuardrails] = useState<string[]>([]);
  const [draftDefaultTemplate, setDraftDefaultTemplate] = useState("");
  const [draftModelId, setDraftModelId] = useState("");
  const [exportDataMode, setExportDataMode] = useState<AnalyticsProjectDataFileMode>("copy");
  const [exportPlan, setExportPlan] = useState<AnalyticsProjectExportPlan | null>(null);
  const [exportPlanLoading, setExportPlanLoading] = useState(false);
  const [exportReady, setExportReady] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const [exportSuccess, setExportSuccess] = useState("");
  const [editorNotice, setEditorNotice] = useState("");
  const fileTree = useMemo(() => buildFileTree(files), [files]);
  const [collapsedDirectories, setCollapsedDirectories] = useState<Set<string>>(() => new Set());
  const mainFile = useMemo(() => files.find((file) => file.main) || files.find((file) => file.relative_path === "model.md") || null, [files]);

  useEffect(() => {
    if (!selectedFile) return;
    const directoryPaths = fileDirectoryPaths(selectedFile.relative_path || selectedFile.name);
    if (!directoryPaths.length) return;
    setCollapsedDirectories((current) => {
      if (!directoryPaths.some((path) => current.has(path))) return current;
      const next = new Set(current);
      directoryPaths.forEach((path) => next.delete(path));
      return next;
    });
  }, [selectedFile]);

  useEffect(() => {
    if (mode !== "export" || !model) return;
    let cancelled = false;
    setExportPlanLoading(true);
    setExportPlan(null);
    setExportReady(false);
    setExportError("");
    setExportSuccess("");
    planAnalyticsProjectExport(model.id, exportDataMode)
      .then((result) => {
        if (cancelled) return;
        setExportPlan(result.plan);
        setExportReady(result.ready);
      })
      .catch((error) => {
        if (cancelled) return;
        setExportPlan(null);
        setExportReady(false);
        setExportError(error instanceof Error ? error.message : "无法生成导出计划");
      })
      .finally(() => {
        if (!cancelled) setExportPlanLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [exportDataMode, mode, model]);

  const toggleDirectory = (path: string) => {
    setCollapsedDirectories((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const renderFileTreeNodes = (nodes: FileTreeNode<AnalyticsModelFile>[], depth = 0): ReactNode =>
    nodes.map((node) => {
      if (node.kind === "directory") {
        const collapsed = collapsedDirectories.has(node.path);
        return (
          <div key={node.path}>
            <button
              type="button"
              onClick={() => toggleDirectory(node.path)}
              aria-expanded={!collapsed}
              className="flex h-9 w-full items-center gap-1.5 rounded-xl pr-2 text-left text-xs font-semibold text-gray-600 transition hover:bg-white hover:text-gray-900"
              style={{ paddingLeft: `${8 + depth * 18}px` }}
            >
              {collapsed ? <ChevronRight className="h-3.5 w-3.5 shrink-0 text-gray-400" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-gray-400" />}
              {collapsed ? <Folder className="h-4 w-4 shrink-0 text-[#002fa7]/70" /> : <FolderOpen className="h-4 w-4 shrink-0 text-[#002fa7]/70" />}
              <span className="min-w-0 truncate">{node.name}</span>
            </button>
            {!collapsed ? renderFileTreeNodes(node.children, depth + 1) : null}
          </div>
        );
      }

      const active = selectedFile?.path === node.file.path;
      return (
        <button
          type="button"
          key={node.file.path}
          onClick={() => {
            if (fileDirty && selectedFile?.path !== node.file.path) {
              setEditorNotice("当前文件有未保存修改；请先保存，再切换文件或返回配置编辑。");
              return;
            }
            setEditorNotice("");
            onSelectFile(node.file);
          }}
          title={node.path}
          className={`flex h-9 w-full items-center gap-1.5 rounded-xl pr-2 text-left text-xs font-semibold transition ${
            active ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-600 hover:bg-white"
          }`}
          style={{ paddingLeft: `${8 + depth * 18}px` }}
        >
          <span aria-hidden className="h-3.5 w-3.5 shrink-0" />
          <FileText className={`h-4 w-4 shrink-0 ${active ? "text-[#002fa7]" : "text-[#002fa7]/70"}`} />
          <span className="min-w-0 truncate">{node.name}</span>
        </button>
      );
    });

  useEffect(() => {
    if (!model) return;
    const frontmatter = model.frontmatter || {};
    const dataAssets = frontmatter.data_assets as { tables?: unknown } | undefined;
    const semantic = frontmatter.semantic_assets as { measures?: unknown; dimensions?: unknown; grains?: unknown } | undefined;
    setDraftName(model.name || "");
    setDraftVersion(model.version || "0.1.0");
    setDraftDescription(model.description || "");
    setDraftTags((model.tags || []).join(", "));
    setDraftTables(Array.isArray(dataAssets?.tables) ? dataAssets.tables.map(String) : []);
    setDraftMeasures(Array.isArray(semantic?.measures) ? semantic.measures.map(String) : []);
    setDraftDimensions(Array.isArray(semantic?.dimensions) ? semantic.dimensions.map(String) : []);
    setDraftGrains(Array.isArray(semantic?.grains) ? semantic.grains.map(String) : []);
    setDraftRelations(Array.isArray(model.asset_relations) ? model.asset_relations.map(String) : []);
    setDraftGuardrails(Array.isArray(model.guardrails) ? model.guardrails.map(String) : []);
    setDraftDefaultTemplate(model.default_template || "");
    setDraftModelId(model.id);
  }, [model]);

  const tableOptions = useMemo(() => {
    const options: Array<{ value: string; label: string; hint: string }> = [];
    tableAssets.forEach((asset) => {
      options.push({ value: `table_asset:${asset.asset_id}`, label: asset.file_name, hint: sourceTypeLabel(asset) });
    });
    databaseSources.forEach((source) => {
      (source.selected_tables || []).forEach((table) => {
        options.push({ value: `${source.id}.${table}`, label: table, hint: source.name || source.id });
      });
    });
    const seen = new Set<string>();
    return options.filter((option) => {
      if (seen.has(option.value)) return false;
      seen.add(option.value);
      return true;
    });
  }, [databaseSources, tableAssets]);
  const measures = semanticAssets.filter((asset) => asset.type === "measure");
  const dimensions = semanticAssets.filter((asset) => asset.type === "dimension");
  const grains = semanticAssets.filter((asset) => asset.type === "grain");
  const relationAssets = semanticAssets.filter((asset) => asset.type === "relation");
  const availableRelations = relationAssets.filter((asset) => {
    const relation = asset.relation_definition;
    if (!relation) return false;
    if (asset.relation_type === "dimension_binding") {
      return Boolean(draftTables.length > 1 && relation.asset?.ref && relation.dimension?.ref && draftTables.includes(relation.asset.ref));
    }
    if (asset.relation_type === "direct_join") {
      return Boolean(relation.left?.ref && relation.right?.ref && draftTables.includes(relation.left.ref) && draftTables.includes(relation.right.ref));
    }
    return false;
  });
  const configDirty = useMemo(() => {
    if (!model || draftModelId !== model.id) return false;
    const frontmatter = model.frontmatter || {};
    const dataAssets = frontmatter.data_assets as { tables?: unknown } | undefined;
    const semantic = frontmatter.semantic_assets as { measures?: unknown; dimensions?: unknown; grains?: unknown } | undefined;
    const persisted = {
      name: model.name || "",
      version: model.version || "0.1.0",
      description: model.description || "",
      tags: model.tags || [],
      tables: Array.isArray(dataAssets?.tables) ? dataAssets.tables.map(String) : [],
      measures: Array.isArray(semantic?.measures) ? semantic.measures.map(String) : [],
      dimensions: Array.isArray(semantic?.dimensions) ? semantic.dimensions.map(String) : [],
      grains: Array.isArray(semantic?.grains) ? semantic.grains.map(String) : [],
      relations: Array.isArray(model.asset_relations) ? model.asset_relations.map(String) : [],
      guardrails: Array.isArray(model.guardrails) ? model.guardrails.map(String) : [],
      defaultTemplate: model.default_template || "",
    };
    const current = {
      name: draftName,
      version: draftVersion,
      description: draftDescription,
      tags: splitTokenList(draftTags),
      tables: draftTables,
      measures: draftMeasures,
      dimensions: draftDimensions,
      grains: draftGrains,
      relations: draftRelations,
      guardrails: draftGuardrails,
      defaultTemplate: draftDefaultTemplate,
    };
    return JSON.stringify(current) !== JSON.stringify(persisted);
  }, [
    draftDefaultTemplate, draftDescription, draftDimensions, draftGrains, draftGuardrails,
    draftMeasures, draftModelId, draftName, draftRelations, draftTables, draftTags, draftVersion, model,
  ]);
  const dirty = fileDirty || configDirty;
  useEffect(() => {
    if (!model || draftModelId !== model.id) return;
    const valid = new Set(availableRelations.map((asset) => asset.id));
    setDraftRelations((current) => current.filter((relationId) => valid.has(relationId)));
  }, [draftModelId, draftTables, model, semanticAssets]);
  const toggle = (values: string[], value: string, setter: (next: string[]) => void) => {
    setter(values.includes(value) ? values.filter((item) => item !== value) : [...values, value]);
  };
  const toggleDimension = (dimensionId: string) => {
    if (!draftDimensions.includes(dimensionId)) {
      setDraftDimensions([...draftDimensions, dimensionId]);
      return;
    }
    setDraftDimensions(draftDimensions.filter((item) => item !== dimensionId));
    const boundRelationIds = new Set(
      relationAssets
        .filter((asset) => asset.relation_type === "dimension_binding" && asset.relation_definition?.dimension?.ref === dimensionId)
        .map((asset) => asset.id)
    );
    setDraftRelations((current) => current.filter((relationId) => !boundRelationIds.has(relationId)));
  };
  const toggleRelation = (relationId: string) => {
    if (draftRelations.includes(relationId)) {
      setDraftRelations(draftRelations.filter((item) => item !== relationId));
      return;
    }
    const relation = relationAssets.find((asset) => asset.id === relationId);
    const dimensionRef = relation?.relation_type === "dimension_binding" ? relation.relation_definition?.dimension?.ref : undefined;
    if (dimensionRef && !draftDimensions.includes(dimensionRef)) {
      setDraftDimensions([...draftDimensions, dimensionRef]);
    }
    setDraftRelations([...draftRelations, relationId]);
  };
  const setAllDraftDimensions = (nextDimensions: string[]) => {
    const selected = new Set(nextDimensions);
    setDraftDimensions(nextDimensions);
    setDraftRelations((current) => current.filter((relationId) => {
      const relation = relationAssets.find((asset) => asset.id === relationId);
      const dimensionRef = relation?.relation_type === "dimension_binding" ? relation.relation_definition?.dimension?.ref : undefined;
      return !dimensionRef || selected.has(dimensionRef);
    }));
  };
  const setAllDraftRelations = (nextRelations: string[]) => {
    setDraftRelations(nextRelations);
    const requiredDimensions = relationAssets
      .filter((asset) => nextRelations.includes(asset.id) && asset.relation_type === "dimension_binding")
      .map((asset) => asset.relation_definition?.dimension?.ref)
      .filter((value): value is string => Boolean(value));
    if (requiredDimensions.length) {
      setDraftDimensions((current) => Array.from(new Set([...current, ...requiredDimensions])));
    }
  };
  const saveConfig = () => {
    if (!model || !mainFile || selectedFile?.path !== mainFile.path) return;
    const nextContent = buildAnalyticsModelContent(
      model,
      {
        name: draftName,
        version: draftVersion,
        description: draftDescription,
        tags: splitTokenList(draftTags),
        tables: draftTables,
        measures: draftMeasures,
        dimensions: draftDimensions,
        grains: draftGrains,
        assetRelations: draftRelations,
        guardrails: draftGuardrails,
        defaultTemplate: draftDefaultTemplate,
      },
      editorContent
    );
    onChangeContent(nextContent);
    onSave(nextContent);
  };

  const downloadAnalysisProject = () => {
    if (!model || dirty || !exportReady) return;
    setExporting(true);
    setExportError("");
    setExportSuccess("");
    const anchor = document.createElement("a");
    anchor.href = analyticsProjectExportDownloadUrl(model.id, exportDataMode, exportPlan?.plan_id);
    anchor.download = "";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setExportSuccess("下载已开始；大文件会由浏览器直接流式保存。请保留本页直到下载任务出现。");
    window.setTimeout(() => {
      setExporting(false);
    }, 500);
  };

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="flex max-h-[88vh] w-full max-w-6xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {model ? <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-semibold text-[#002fa7]">分析模型</span> : null}
              <h3 className="truncate text-lg font-semibold text-gray-950">{model?.name || "分析模型"}</h3>
              {model?.version ? <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-semibold text-gray-500">v{model.version}</span> : null}
              {dirty ? <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-700">未保存</span> : null}
            </div>
            <p className="mt-1 truncate text-sm text-gray-500">{model?.path || "正在加载..."}</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900">
            <X className="h-5 w-5" />
          </button>
        </div>

        {loading ? (
          <div className="flex min-h-[460px] items-center justify-center text-sm text-gray-400">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            正在读取分析模型…
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            <div className="flex items-center justify-between gap-3 border-b border-black/[0.06] px-5 py-3">
              <div className="inline-flex rounded-2xl bg-gray-100 p-1">
                {[
                  { value: "config", label: "配置编辑" },
                  { value: "file", label: "模型明细" },
                  { value: "export", label: "导出项目" },
                ].map((item) => (
                  <button
                    key={item.value}
                    type="button"
                    onClick={() => {
                      const nextMode = item.value as "config" | "file" | "export";
                      if (nextMode === "config" && mainFile && selectedFile?.path !== mainFile.path) {
                        if (fileDirty) {
                          setEditorNotice("当前文件有未保存修改；请先保存，再返回配置编辑。");
                          return;
                        }
                        setEditorNotice("");
                        onSelectFile(mainFile);
                      }
                      setMode(nextMode);
                    }}
                    className={`h-9 rounded-xl px-4 text-sm font-semibold transition ${
                      mode === item.value ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500"
                    }`}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              {mode === "config" ? (
                <button
                  type="button"
                  disabled={fileDirty || editorLoading || editorSaving || !configDirty || !mainFile || selectedFile?.path !== mainFile.path}
                  onClick={saveConfig}
                  className="inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-3 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {editorSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                  保存配置
                </button>
              ) : mode === "file" ? (
                <button
                  type="button"
                  disabled={!selectedFile?.editable || !dirty || editorLoading || editorSaving}
                  onClick={() => onSave()}
                  className="inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-3 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {editorSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                  保存文件
                </button>
              ) : (
                <button
                  type="button"
                  disabled={dirty || !exportReady || exportPlanLoading || exporting}
                  onClick={downloadAnalysisProject}
                  className="inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-3 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {exporting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                  {exporting ? "正在打包" : "导出 ZIP"}
                </button>
              )}
            </div>

            {editorNotice ? (
              <div className="flex items-center gap-2 border-b border-amber-200 bg-amber-50 px-5 py-2.5 text-xs font-medium text-amber-800">
                <AlertCircle className="h-4 w-4 shrink-0" />
                {editorNotice}
              </div>
            ) : null}

            {mode === "config" ? (
              <div className="grid min-h-0 flex-1 gap-5 overflow-y-auto p-5 lg:grid-cols-2">
                <section className="space-y-4">
                  <div className="rounded-3xl border border-black/[0.06] p-4">
                    <h4 className="text-sm font-semibold text-gray-950">基础信息</h4>
                    <div className="mt-3 grid gap-3 md:grid-cols-2">
                      <label className="block text-sm font-semibold text-gray-700">
                        名称
                        <input
                          value={draftName}
                          onChange={(event) => setDraftName(event.target.value)}
                          className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                        />
                      </label>
                      <label className="block text-sm font-semibold text-gray-700">
                        版本
                        <input
                          value={draftVersion}
                          onChange={(event) => setDraftVersion(event.target.value)}
                          className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                        />
                      </label>
                      <label className="block text-sm font-semibold text-gray-700 md:col-span-2">
                        描述
                        <textarea
                          value={draftDescription}
                          onChange={(event) => setDraftDescription(event.target.value)}
                          className="mt-2 min-h-24 w-full rounded-2xl border border-black/[0.08] bg-white px-3 py-2 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                        />
                      </label>
                      <label className="block text-sm font-semibold text-gray-700">
                        标签
                        <input
                          value={draftTags}
                          onChange={(event) => setDraftTags(event.target.value)}
                          className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                          placeholder="多个标签用逗号分隔"
                        />
                      </label>
                      <label className="block text-sm font-semibold text-gray-700">
                        默认模板
                        <input
                          value={draftDefaultTemplate}
                          onChange={(event) => setDraftDefaultTemplate(event.target.value)}
                          className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                          placeholder="templates/report.md"
                        />
                      </label>
                    </div>
                  </div>
                  <ModelOptionPicker
                    title="数据资产"
                    emptyText="暂无可选表。"
                    options={tableOptions}
                    values={draftTables}
                    onToggle={(value) => toggle(draftTables, value, setDraftTables)}
                    onToggleAll={setDraftTables}
                  />
                </section>
                <section className="space-y-4">
                  <ModelOptionPicker
                    title="度量值"
                    emptyText="暂无度量值。"
                    options={measures.map((asset) => ({ value: asset.id, label: asset.name, hint: asset.id }))}
                    values={draftMeasures}
                    onToggle={(value) => toggle(draftMeasures, value, setDraftMeasures)}
                    onToggleAll={setDraftMeasures}
                  />
                  <ModelOptionPicker
                    title="维度"
                    emptyText="暂无维度。"
                    options={dimensions.map((asset) => ({ value: asset.id, label: asset.name, hint: asset.id }))}
                    values={draftDimensions}
                    onToggle={toggleDimension}
                    onToggleAll={setAllDraftDimensions}
                  />
                  <ModelOptionPicker
                    title="颗粒度"
                    emptyText="暂无颗粒度。"
                    options={grains.map((asset) => ({ value: asset.id, label: asset.name, hint: asset.id }))}
                    values={draftGrains}
                    onToggle={(value) => toggle(draftGrains, value, setDraftGrains)}
                    onToggleAll={setDraftGrains}
                  />
                  <ModelOptionPicker
                    title="资产关联"
                    emptyText={draftTables.length > 1 ? "所选数据资产之间没有可用的已发布关联。" : "选择至少两个数据资产后显示可用关联。"}
                    options={availableRelations.map((asset) => ({ value: asset.id, label: asset.name, hint: asset.relation_type === "dimension_binding" ? "关联维度" : "字段关联" }))}
                    values={draftRelations}
                    onToggle={toggleRelation}
                    onToggleAll={setAllDraftRelations}
                  />
                  <ModelOptionPicker
                    title="SQL 守卫"
                    emptyText="暂无 SQL 守卫。"
                    options={sqlGuardrails.map((rule) => ({ value: rule.id, label: rule.name, hint: rule.type }))}
                    values={draftGuardrails}
                    onToggle={(value) => toggle(draftGuardrails, value, setDraftGuardrails)}
                    onToggleAll={setDraftGuardrails}
                  />
                </section>
              </div>
            ) : mode === "file" ? (
              <div className="grid min-h-0 flex-1 grid-cols-[260px_minmax(0,1fr)] overflow-hidden">
                <aside className="min-h-0 border-r border-black/[0.06] bg-slate-50/70 p-4">
                  <div className="max-h-[620px] space-y-1 overflow-auto">
                    {renderFileTreeNodes(fileTree)}
                    {fileTree.length === 0 ? (
                      <div className="rounded-xl border border-dashed border-black/[0.08] px-3 py-8 text-center text-xs text-gray-400">
                        没有文件
                      </div>
                    ) : null}
                  </div>
                </aside>
                <section className="flex min-h-0 flex-col">
                  <div className="border-b border-black/[0.06] px-5 py-3">
                    <p className="truncate text-sm font-semibold text-gray-900">{selectedFile?.relative_path || selectedFile?.name || "未选择文件"}</p>
                    <p className="mt-0.5 text-xs text-gray-400">手工编辑 model.md 后保存会刷新模型 registry。</p>
                  </div>
                  <div className="relative min-h-[520px] flex-1">
                    <textarea
                      value={editorContent}
                      onChange={(event) => onChangeContent(event.target.value)}
                      disabled={!selectedFile?.editable || editorLoading}
                      aria-busy={editorLoading}
                      spellCheck={false}
                      className="absolute inset-0 h-full w-full resize-none bg-white p-5 font-mono text-sm leading-6 text-gray-800 outline-none disabled:text-gray-400"
                    />
                    {editorLoading ? (
                      <div className="absolute inset-0 flex items-center justify-center bg-white text-sm text-gray-400">
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        正在读取文件…
                      </div>
                    ) : null}
                  </div>
                </section>
              </div>
            ) : (
              <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50/50 p-5">
                <div className="mx-auto max-w-4xl space-y-5">
                  <section className="rounded-3xl border border-black/[0.06] bg-white p-5 shadow-sm shadow-black/[0.02]">
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <h4 className="text-base font-semibold text-gray-950">可迁移分析项目</h4>
                        <p className="mt-1 max-w-2xl text-sm leading-6 text-gray-500">
                          导出模型、语义资产、关联、SQL 守卫、Profile 与本地校验脚本。解压后可直接作为 Codex 等本地 Agent 的项目目录打开。
                        </p>
                      </div>
                      <span className="shrink-0 rounded-full bg-emerald-50 px-3 py-1 text-xs font-semibold text-emerald-700">
                        analysis-project/v1
                      </span>
                    </div>

                    <div className="mt-5 grid gap-3 md:grid-cols-2">
                      {([
                        {
                          value: "copy" as const,
                          title: "复制数据文件",
                          description: "将 Excel、CSV、Parquet 等文件一并打包，适合跨机器迁移。",
                          recommended: true,
                        },
                        {
                          value: "reference" as const,
                          title: "保留本机路径",
                          description: "不复制大文件，在 bindings.local.yaml 中记录绝对路径与 hash。",
                          recommended: false,
                        },
                      ]).map((option) => {
                        const active = exportDataMode === option.value;
                        return (
                          <button
                            key={option.value}
                            type="button"
                            onClick={() => setExportDataMode(option.value)}
                            className={`rounded-2xl border p-4 text-left transition ${
                              active
                                ? "border-[#002fa7]/35 bg-[#002fa7]/[0.04] ring-4 ring-[#002fa7]/[0.05]"
                                : "border-black/[0.07] bg-white hover:border-black/[0.14]"
                            }`}
                          >
                            <div className="flex items-center justify-between gap-3">
                              <span className={`text-sm font-semibold ${active ? "text-[#002fa7]" : "text-gray-900"}`}>
                                {option.title}
                              </span>
                              {option.recommended ? (
                                <span className="rounded-full bg-[#002fa7]/10 px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">推荐</span>
                              ) : null}
                            </div>
                            <p className="mt-2 text-xs leading-5 text-gray-500">{option.description}</p>
                          </button>
                        );
                      })}
                    </div>
                  </section>

                  {dirty ? (
                    <div className="flex gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                      <div>
                        <p className="font-semibold">请先保存当前模型改动</p>
                        <p className="mt-1 text-xs leading-5 text-amber-700">导出器只读取 Registry 中已保存的版本，避免将编辑器草稿与磁盘依赖混合打包。</p>
                      </div>
                    </div>
                  ) : null}

                  <section className="rounded-3xl border border-black/[0.06] bg-white p-5 shadow-sm shadow-black/[0.02]">
                    <div className="flex items-center justify-between gap-4">
                      <div>
                        <h4 className="text-sm font-semibold text-gray-950">导出清单</h4>
                        <p className="mt-1 text-xs text-gray-500">每次切换数据策略都会重新解析依赖，不依赖前端估算。</p>
                      </div>
                      {exportPlanLoading ? (
                        <span className="inline-flex items-center gap-2 text-xs text-gray-400"><Loader2 className="h-4 w-4 animate-spin" />正在解析依赖</span>
                      ) : exportReady ? (
                        <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-emerald-700"><CheckCircle2 className="h-4 w-4" />可以导出</span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-rose-700"><AlertCircle className="h-4 w-4" />依赖不完整</span>
                      )}
                    </div>

                    {exportPlan ? (
                      <>
                        <div className="mt-4 grid gap-3 sm:grid-cols-4">
                          {[
                            { label: "语义资产", value: exportPlan.semantic_asset_ids.length },
                            { label: "资产关联", value: exportPlan.relation_ids.length },
                            { label: "SQL 守卫", value: exportPlan.guardrail_ids.length },
                            {
                              label: exportDataMode === "copy" ? "复制数据" : "本地绑定",
                              value: exportDataMode === "copy" ? formatBytes(exportPlan.copied_bytes) : exportPlan.data_assets.length,
                            },
                          ].map((item) => (
                            <div key={item.label} className="rounded-2xl bg-slate-50 px-4 py-3">
                              <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">{item.label}</p>
                              <p className="mt-1 text-lg font-semibold text-gray-950">{item.value}</p>
                            </div>
                          ))}
                        </div>

                        <div className="mt-4 space-y-2">
                          {exportPlan.data_assets.map((asset) => (
                            <div key={asset.ref} className="flex items-center gap-3 rounded-2xl border border-black/[0.05] px-4 py-3">
                              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-slate-100 text-gray-500">
                                {asset.kind === "database_table" ? <Database className="h-4 w-4" /> : <FileSpreadsheet className="h-4 w-4" />}
                              </div>
                              <div className="min-w-0 flex-1">
                                <p className="truncate text-sm font-semibold text-gray-900">{asset.file_name || asset.ref}</p>
                                <p className="mt-0.5 truncate text-xs text-gray-400">
                                  {asset.kind === "database_table"
                                    ? "数据库连接以环境变量占位，不导出密码"
                                    : exportDataMode === "copy"
                                      ? `${asset.sheet_name || "文件资产"} · ${formatBytes(asset.size_bytes)}`
                                      : asset.source_path}
                                </p>
                              </div>
                              <span className={`rounded-full px-2 py-1 text-[11px] font-semibold ${asset.status === "ready" ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-700"}`}>
                                {asset.status === "ready" ? "就绪" : "缺失"}
                              </span>
                            </div>
                          ))}
                          {exportPlan.data_assets.length === 0 ? (
                            <div className="rounded-2xl border border-dashed border-black/[0.08] px-4 py-6 text-center text-xs text-gray-400">模型未声明数据资产</div>
                          ) : null}
                        </div>
                      </>
                    ) : null}

                    {exportPlan?.warnings.length ? (
                      <div className="mt-4 rounded-2xl bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-800">
                        {exportPlan.warnings.map((warning) => <p key={warning}>• {warning}</p>)}
                      </div>
                    ) : null}
                    {exportPlan?.missing_dependencies.length ? (
                      <div className="mt-4 rounded-2xl bg-rose-50 px-4 py-3 text-xs leading-5 text-rose-800">
                        <p className="font-semibold">缺少以下必需依赖：</p>
                        {exportPlan.missing_dependencies.map((dependency) => <p key={dependency}>• {dependency}</p>)}
                      </div>
                    ) : null}
                    {exportError ? <p className="mt-4 text-sm font-medium text-rose-700">{exportError}</p> : null}
                    {exportSuccess ? <p className="mt-4 text-sm font-medium text-emerald-700">{exportSuccess}</p> : null}
                  </section>

                  <section className="grid gap-3 md:grid-cols-3">
                    {[
                      [ShieldCheck, "口径同源", "Measure、Dimension、Grain、Reference 与 Relation 原文件随包交付。"],
                      [Database, "绑定解耦", "PuddingClaw ID 只保留为 provenance，外部执行读取 bindings。"],
                      [CheckCircle2, "确定性校验", "文件 hash、项目完整性和可移植 SQL Guardrail 校验器一并生成。"],
                    ].map(([Icon, title, description]) => {
                      const FeatureIcon = Icon as LucideIcon;
                      return (
                        <div key={String(title)} className="rounded-2xl border border-black/[0.06] bg-white p-4">
                          <FeatureIcon className="h-5 w-5 text-[#002fa7]" />
                          <p className="mt-3 text-sm font-semibold text-gray-900">{String(title)}</p>
                          <p className="mt-1 text-xs leading-5 text-gray-500">{String(description)}</p>
                        </div>
                      );
                    })}
                  </section>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function SemanticAssetCreateModal({
  busy,
  tableAssets,
  databaseSources,
  semanticAssets,
  initialType,
  onClose,
  onCreate,
}: {
  busy: boolean;
  tableAssets: TableAsset[];
  databaseSources: KnowledgeDatabaseSource[];
  semanticAssets: SemanticAssetSummary[];
  initialType: SemanticAssetType;
  onClose: () => void;
  onCreate: (payload: {
    name: string;
    type: SemanticAssetType;
    description: string;
    aliases: string[];
    tags: string[];
    version: string;
    dimension_definition?: DimensionDefinition;
    relation_definition?: AssetRelationDefinition;
  }) => void;
}) {
  const [type, setType] = useState<SemanticAssetType>(initialType);
  const [name, setName] = useState("");
  const [version, setVersion] = useState("0.1.0");
  const [description, setDescription] = useState("");
  const [aliases, setAliases] = useState("");
  const [tags, setTags] = useState("");
  const [dimensionMode, setDimensionMode] = useState<DimensionDefinition["mode"]>("source_field");
  const [bindings, setBindings] = useState<Array<{ asset_ref: string; display_name: string; fields: string }>>([
    { asset_ref: "", display_name: "", fields: "" },
  ]);
  const [derivedExpression, setDerivedExpression] = useState("");
  const [canonicalFields, setCanonicalFields] = useState("canonical_brand, canonical_series");
  const [referencePath, setReferencePath] = useState("");
  const [weekStartDay, setWeekStartDay] = useState("monday");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [relationType, setRelationType] = useState<"dimension_binding" | "direct_join">("dimension_binding");
  const [relationAssetRef, setRelationAssetRef] = useState("");
  const [relationAssetFields, setRelationAssetFields] = useState<string[]>([]);
  const [relationDimensionRef, setRelationDimensionRef] = useState("");
  const [relationLeftRef, setRelationLeftRef] = useState("");
  const [relationLeftFields, setRelationLeftFields] = useState<string[]>([]);
  const [relationRightRef, setRelationRightRef] = useState("");
  const [relationRightFields, setRelationRightFields] = useState<string[]>([]);
  const [relationCardinality, setRelationCardinality] = useState<AssetRelationDefinition["cardinality"]>("many_to_one");
  const typeLabel = type === "measure" ? "度量值" : type === "grain" ? "颗粒度" : type === "relation" ? "资产关联" : "维度";
  const isDimension = type === "dimension";
  const isRelation = type === "relation";
  const sourceOptions = [
    ...tableAssets.map((asset) => ({ value: `table_asset:${asset.asset_id}`, label: `${asset.file_name}${asset.sheet_name ? ` · ${asset.sheet_name}` : ""}` })),
    ...databaseSources.flatMap((source) =>
      source.selected_tables.length
        ? source.selected_tables.map((table) => ({ value: `${source.id}.${table}`, label: `${source.name} · ${table}` }))
        : [{ value: `database_source:${source.id}`, label: `${source.name} · 未选择表` }]
    ),
  ];
  const { columnsByAsset, loadingByAsset, reloadColumns } = useDimensionBindingColumns(
    [
      ...(dimensionMode === "entity_lookup" ? [] : bindings),
      { asset_ref: relationAssetRef },
      { asset_ref: relationLeftRef },
      { asset_ref: relationRightRef },
    ],
    tableAssets,
    databaseSources
  );
  const updateBinding = (index: number, changes: Partial<{ asset_ref: string; display_name: string; fields: string }>) => {
    setBindings((current) => current.map((item, itemIndex) => (itemIndex === index ? { ...item, ...changes } : item)));
  };
  const buildRelationDefinition = (): AssetRelationDefinition => {
    if (relationType === "dimension_binding") {
      return {
        type: relationType,
        asset: { ref: relationAssetRef, display_name: sourceOptions.find((item) => item.value === relationAssetRef)?.label, key_fields: relationAssetFields },
        dimension: { ref: relationDimensionRef, display_name: semanticAssets.find((item) => item.id === relationDimensionRef)?.name, output_key: "entity_key" },
        cardinality: "many_to_one",
        grain: relationAssetFields,
        use_statuses: ["auto_matched", "accepted"],
      };
    }
    return {
      type: relationType,
      left: { ref: relationLeftRef, display_name: sourceOptions.find((item) => item.value === relationLeftRef)?.label, key_fields: relationLeftFields },
      right: { ref: relationRightRef, display_name: sourceOptions.find((item) => item.value === relationRightRef)?.label, key_fields: relationRightFields },
      field_mapping: { left: relationLeftFields, right: relationRightFields },
      cardinality: relationCardinality,
      join_type: "left",
      grain: { left: relationLeftFields, right: relationRightFields },
    };
  };
  const buildDimensionDefinition = (): DimensionDefinition => {
    const activeBindings = dimensionMode === "entity_lookup" ? bindings : bindings.slice(0, 1);
    const normalizedBindings = activeBindings.filter((binding) => binding.asset_ref || binding.fields.trim()).map((binding) => {
      const fields = splitTokenList(binding.fields);
      const mappedFields = dimensionMode === "source_field" || dimensionMode === "derived"
        ? { value: fields[0] || "" }
        : dimensionMode === "calendar_lookup"
          ? { date: fields[0] || "" }
          : Object.fromEntries(fields.map((field, index) => [`key_${index + 1}`, field]));
      return { asset_ref: binding.asset_ref, display_name: binding.display_name, fields: mappedFields };
    });
    const definition: DimensionDefinition = { mode: dimensionMode, bindings: normalizedBindings };
    if (dimensionMode === "derived") {
      definition.source_fields = splitTokenList(activeBindings.flatMap((binding) => splitTokenList(binding.fields)).join(","));
      definition.expression = derivedExpression;
    }
    if (dimensionMode === "entity_lookup") {
      definition.canonical = { key: "entity_key", fields: splitTokenList(canonicalFields) };
      definition.reference_path = referencePath.trim();
    }
    if (dimensionMode === "calendar_lookup") {
      definition.date_field = splitTokenList(bindings[0]?.fields || "")[0] || "";
      definition.week_start_day = weekStartDay;
      definition.timezone = timezone;
    }
    return definition;
  };

  const templatePreview = `---
formatter: semantic-asset
name: ${name || typeLabel}
type: ${type}
description: ${description || "在这里描述业务口径"}
aliases: []
tags: []
${isDimension ? `resolution_mode: ${dimensionMode}\nresolution:\n  mode: ${dimensionMode}\n` : ""}${isRelation ? `relation_type: ${relationType}\nrelation: {}\n` : ""}version: ${version || "0.1.0"}
created: YYYY-MM-DD HH:mm:ss
updated_at: YYYY-MM-DD HH:mm:ss
---`;

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-950">新建语义资产</h3>
            <p className="mt-1 text-sm text-gray-500">生成 measure.md、grain.md、dimension.md 或 relation.md，后端会立即刷新 registry。</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="grid min-h-0 gap-5 overflow-auto px-6 py-5 md:grid-cols-[minmax(0,1fr)_300px]">
          <div className="space-y-3">
            <label className="block text-sm font-semibold text-gray-700">
              类型
              <select
                value={type}
                onChange={(event) => setType(event.target.value as SemanticAssetType)}
                className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              >
                <option value="measure">度量值 measure</option>
                <option value="grain">颗粒度 grain</option>
                <option value="dimension">维度 dimension</option>
                <option value="relation">资产关联 relation</option>
              </select>
            </label>
            {isDimension ? (
              <DimensionDefinitionFields
                mode={dimensionMode}
                bindings={bindings}
                sourceOptions={sourceOptions}
                fieldOptionsByAsset={columnsByAsset}
                fieldOptionsLoading={loadingByAsset}
                onReloadFields={reloadColumns}
                derivedExpression={derivedExpression}
                canonicalFields={canonicalFields}
                referencePath={referencePath}
                weekStartDay={weekStartDay}
                timezone={timezone}
                onModeChange={setDimensionMode}
                onBindingChange={updateBinding}
                onAddBinding={() => setBindings((current) => [...current, { asset_ref: "", display_name: "", fields: "" }])}
                onRemoveBinding={(index) => setBindings((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                onDerivedExpressionChange={setDerivedExpression}
                onCanonicalFieldsChange={setCanonicalFields}
                onReferencePathChange={setReferencePath}
                onWeekStartDayChange={setWeekStartDay}
                onTimezoneChange={setTimezone}
              />
            ) : null}
            {isRelation ? (
              <div className="space-y-3 rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.025] p-4">
                <div className="grid gap-2 sm:grid-cols-2">
                  <button type="button" onClick={() => setRelationType("dimension_binding")} className={`rounded-xl border p-3 text-left ${relationType === "dimension_binding" ? "border-[#002fa7] bg-white text-[#002fa7]" : "border-black/[0.08] bg-white text-slate-600"}`}><p className="font-semibold">关联维度</p><p className="mt-1 text-xs leading-5">一张资产接入已发布维度</p></button>
                  <button type="button" onClick={() => setRelationType("direct_join")} className={`rounded-xl border p-3 text-left ${relationType === "direct_join" ? "border-[#002fa7] bg-white text-[#002fa7]" : "border-black/[0.08] bg-white text-slate-600"}`}><p className="font-semibold">字段关联</p><p className="mt-1 text-xs leading-5">两张资产使用稳定业务键</p></button>
                </div>
                {relationType === "dimension_binding" ? <div className="grid gap-3 sm:grid-cols-2"><label className="text-xs font-semibold text-slate-600">数据资产<select value={relationAssetRef} onChange={(event) => { setRelationAssetRef(event.target.value); setRelationAssetFields([]); }} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] bg-white px-3 text-sm font-normal"><option value="">选择资产</option>{sourceOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><FieldMultiSelect label="来源键字段" values={relationAssetFields} disabled={!relationAssetRef || loadingByAsset[relationAssetRef]} options={(columnsByAsset[relationAssetRef] || []).map((field) => ({ value: field, label: field }))} onChange={setRelationAssetFields} /><label className="sm:col-span-2 text-xs font-semibold text-slate-600">已发布维度<select value={relationDimensionRef} onChange={(event) => setRelationDimensionRef(event.target.value)} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] bg-white px-3 text-sm font-normal"><option value="">选择维度</option>{semanticAssets.filter((item) => item.type === "dimension").map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div> : <div className="space-y-3"><div className="grid gap-3 sm:grid-cols-2"><label className="text-xs font-semibold text-slate-600">左侧资产<select value={relationLeftRef} onChange={(event) => { setRelationLeftRef(event.target.value); setRelationLeftFields([]); }} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] bg-white px-3 text-sm font-normal"><option value="">选择资产</option>{sourceOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><FieldMultiSelect label="左侧键字段" values={relationLeftFields} disabled={!relationLeftRef || loadingByAsset[relationLeftRef]} options={(columnsByAsset[relationLeftRef] || []).map((field) => ({ value: field, label: field }))} onChange={setRelationLeftFields} /><label className="text-xs font-semibold text-slate-600">右侧资产<select value={relationRightRef} onChange={(event) => { setRelationRightRef(event.target.value); setRelationRightFields([]); }} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] bg-white px-3 text-sm font-normal"><option value="">选择资产</option>{sourceOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><FieldMultiSelect label="右侧键字段" values={relationRightFields} disabled={!relationRightRef || loadingByAsset[relationRightRef]} options={(columnsByAsset[relationRightRef] || []).map((field) => ({ value: field, label: field }))} onChange={setRelationRightFields} /></div><label className="block text-xs font-semibold text-slate-600">基数<select value={relationCardinality} onChange={(event) => setRelationCardinality(event.target.value as AssetRelationDefinition["cardinality"])} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] bg-white px-3 text-sm font-normal"><option value="one_to_one">一对一</option><option value="one_to_many">一对多</option><option value="many_to_one">多对一</option><option value="many_to_many">多对多</option></select></label></div>}
              </div>
            ) : null}
            <label className="block text-sm font-semibold text-gray-700">
              名称
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                placeholder={type === "measure" ? "配置率" : type === "grain" ? "款型颗粒度" : "上市时间"}
              />
            </label>
            <label className="block text-sm font-semibold text-gray-700">
              版本
              <input
                value={version}
                onChange={(event) => setVersion(event.target.value)}
                className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                placeholder="0.1.0"
              />
            </label>
            <label className="block text-sm font-semibold text-gray-700">
              描述
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                className="mt-2 min-h-24 w-full rounded-2xl border border-black/[0.08] bg-white px-3 py-2 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                placeholder="写清楚业务口径、适用字段、禁止推断规则。"
              />
            </label>
            <label className="block text-sm font-semibold text-gray-700">
              别名
              <input
                value={aliases}
                onChange={(event) => setAliases(event.target.value)}
                className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                placeholder="多个别名用逗号分隔"
              />
            </label>
            <label className="block text-sm font-semibold text-gray-700">
              标签
              <input
                value={tags}
                onChange={(event) => setTags(event.target.value)}
                className="mt-2 h-10 w-full rounded-2xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                placeholder="多个标签用逗号分隔"
              />
            </label>
          </div>
          <div className="rounded-3xl bg-gray-950 p-4 text-xs text-gray-100">
            <p className="mb-3 font-semibold text-white">YAML 模板</p>
            <pre className="whitespace-pre-wrap leading-5">{templatePreview}</pre>
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4">
          <button type="button" onClick={onClose} className="h-10 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700">
            取消
          </button>
          <button
            type="button"
            disabled={busy || !name.trim()}
            onClick={() =>
              onCreate({
                name,
                type,
                description,
                aliases: splitTokenList(aliases),
                tags: splitTokenList(tags),
                version: version.trim() || "0.1.0",
                dimension_definition: isDimension ? buildDimensionDefinition() : undefined,
                relation_definition: isRelation ? buildRelationDefinition() : undefined,
              })
            }
            className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            创建
          </button>
        </div>
      </div>
    </div>
  );
}

function useDimensionBindingColumns(
  bindings: Array<{ asset_ref: string }>,
  tableAssets: TableAsset[],
  databaseSources: KnowledgeDatabaseSource[]
) {
  const [columnsByAsset, setColumnsByAsset] = useState<Record<string, string[]>>({});
  const [loadingByAsset, setLoadingByAsset] = useState<Record<string, boolean>>({});
  const columnsRef = useRef<Record<string, string[]>>({});
  const loadingRef = useRef<Record<string, boolean>>({});

  useEffect(() => {
    const assetRefs = Array.from(new Set(bindings.map((binding) => binding.asset_ref).filter(Boolean)));

    void Promise.all(assetRefs.map(async (assetRef) => {
      if (columnsRef.current[assetRef] !== undefined || loadingRef.current[assetRef]) return;
      loadingRef.current[assetRef] = true;
      setLoadingByAsset((current) => ({ ...current, [assetRef]: true }));
      try {
        let columns: string[] = [];
        if (assetRef.startsWith("table_asset:")) {
          const assetId = assetRef.slice("table_asset:".length);
          const asset = tableAssets.find((item) => item.asset_id === assetId) || await getTableAsset(assetId, true);
          columns = (asset.profile?.columns || []).map((column) => column.name).filter(Boolean);
        } else {
          const separator = assetRef.indexOf(".");
          const sourceId = assetRef.slice(0, separator);
          const tableName = assetRef.slice(separator + 1);
          if (sourceId && tableName && databaseSources.some((source) => source.id === sourceId)) {
            columns = await listKnowledgeDatabaseSourceTableColumns(sourceId, tableName);
          }
        }
        columnsRef.current[assetRef] = columns;
        setColumnsByAsset((current) => ({ ...current, [assetRef]: columns }));
      } catch {
        columnsRef.current[assetRef] = [];
        setColumnsByAsset((current) => ({ ...current, [assetRef]: [] }));
      } finally {
        loadingRef.current[assetRef] = false;
        setLoadingByAsset((current) => ({ ...current, [assetRef]: false }));
      }
    }));
  }, [bindings, databaseSources, tableAssets]);

  const reloadColumns = useCallback((assetRef: string) => {
    if (!assetRef) return;
    delete columnsRef.current[assetRef];
    loadingRef.current[assetRef] = false;
    setColumnsByAsset((current) => {
      const next = { ...current };
      delete next[assetRef];
      return next;
    });
    setLoadingByAsset((current) => ({ ...current, [assetRef]: false }));
  }, []);

  return { columnsByAsset, loadingByAsset, reloadColumns };
}

function DimensionDefinitionFields({
  mode,
  bindings,
  sourceOptions,
  fieldOptionsByAsset,
  fieldOptionsLoading,
  onReloadFields,
  derivedExpression,
  canonicalFields,
  referencePath,
  weekStartDay,
  timezone,
  onModeChange,
  onBindingChange,
  onAddBinding,
  onRemoveBinding,
  onDerivedExpressionChange,
  onCanonicalFieldsChange,
  onReferencePathChange,
  onWeekStartDayChange,
  onTimezoneChange,
  embedded = false,
}: {
  mode: DimensionDefinition["mode"];
  bindings: Array<{ asset_ref: string; display_name: string; fields: string }>;
  sourceOptions: Array<{ value: string; label: string }>;
  fieldOptionsByAsset: Record<string, string[]>;
  fieldOptionsLoading: Record<string, boolean>;
  onReloadFields: (assetRef: string) => void;
  derivedExpression: string;
  canonicalFields: string;
  referencePath: string;
  weekStartDay: string;
  timezone: string;
  onModeChange: (mode: DimensionDefinition["mode"]) => void;
  onBindingChange: (index: number, changes: Partial<{ asset_ref: string; display_name: string; fields: string }>) => void;
  onAddBinding: () => void;
  onRemoveBinding: (index: number) => void;
  onDerivedExpressionChange: (value: string) => void;
  onCanonicalFieldsChange: (value: string) => void;
  onReferencePathChange: (value: string) => void;
  onWeekStartDayChange: (value: string) => void;
  onTimezoneChange: (value: string) => void;
  embedded?: boolean;
}) {
  const modes: Array<{ value: DimensionDefinition["mode"]; label: string; hint: string }> = [
    { value: "source_field", label: "直接字段", hint: "读取已确认的来源字段" },
    { value: "derived", label: "推导规则", hint: "按字段和业务规则计算" },
    { value: "entity_lookup", label: "实体匹配", hint: "映射到规范实体键" },
    { value: "calendar_lookup", label: "日历映射", hint: "日期映射到自然周期" },
  ];
  return (
    <section className={`space-y-4 ${embedded ? "" : "rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.025] p-4"}`}>
      <div>
        <p className="text-sm font-semibold text-gray-900">维度创建方式</p>
        <p className="mt-1 text-xs leading-5 text-gray-500">维度保持一个分类；创建方式只描述值从哪里来。复杂的多表清洗由“构建语义维度”技能完成，结果再写入该维度目录。</p>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {modes.map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={() => onModeChange(option.value)}
            className={`rounded-xl border px-3 py-2.5 text-left transition ${
              mode === option.value ? "border-[#002fa7] bg-white text-[#002fa7] shadow-sm" : "border-black/[0.07] bg-white/70 text-gray-600 hover:border-[#002fa7]/35"
            }`}
          >
            <span className="block text-sm font-semibold">{option.label}</span>
            <span className="mt-0.5 block text-xs text-gray-500">{option.hint}</span>
          </button>
        ))}
      </div>
      {mode === "entity_lookup" ? (
        <div className="rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.025] p-4">
          <p className="text-sm font-semibold text-gray-900">由构建语义维度 Skill 管理</p>
          <p className="mt-1 text-xs leading-5 text-gray-500">实体匹配需要清洗来源字段、生成规范实体键并写入 Crosswalk。这里仅展示已构建结果，不能手工追加来源或修改映射。</p>
          <p className="mt-3 rounded-xl bg-white px-3 py-2 text-xs font-medium text-[#002fa7]">请在 Agent 中提出“刷新 / 构建此维度”，由构建语义维度 Skill 完成更新。</p>
          <div className="mt-3 space-y-2">
            {bindings.filter((binding) => binding.asset_ref || binding.fields.trim()).map((binding, index) => (
              <div key={`${binding.asset_ref}-${index}`} className="rounded-xl border border-black/[0.07] bg-white px-3 py-2.5">
                <p className="text-xs font-semibold text-gray-800">来源绑定 {index + 1} · {binding.display_name || binding.asset_ref}</p>
                <p className="mt-1 font-mono text-xs text-gray-500">{binding.fields || "未记录字段"}</p>
              </div>
            ))}
            {!bindings.some((binding) => binding.asset_ref || binding.fields.trim()) ? <p className="text-xs text-gray-400">尚未构建来源绑定。</p> : null}
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 text-xs">
            <div className="rounded-xl border border-black/[0.07] bg-white px-3 py-2"><span className="text-gray-400">规范实体字段</span><p className="mt-1 font-mono text-gray-700">{canonicalFields || "未记录"}</p></div>
            <div className="rounded-xl border border-black/[0.07] bg-white px-3 py-2"><span className="text-gray-400">Crosswalk</span><p className="mt-1 break-all font-mono text-gray-700">{referencePath || "未记录"}</p></div>
          </div>
        </div>
      ) : (
        <div>
          {bindings.slice(0, 1).map((binding, index) => {
            const selectedFields = splitTokenList(binding.fields);
            const availableFields = fieldOptionsByAsset[binding.asset_ref] || [];
            const currentOnlyFields = selectedFields.filter((field) => !availableFields.includes(field));
            const fieldOptions = [...availableFields, ...currentOnlyFields];
            const fieldsLoading = Boolean(binding.asset_ref && fieldOptionsLoading[binding.asset_ref]);
            return (
              <div key={index} className="rounded-2xl border border-black/[0.07] bg-white p-3">
                <p className="text-xs font-semibold text-gray-700">来源绑定</p>
                <div className="mt-2 grid gap-3 md:grid-cols-2">
                  <label className="block">
                    <span className="text-[11px] font-semibold text-gray-400">数据资产</span>
                    <select
                      value={binding.asset_ref}
                      onChange={(event) => {
                        const selected = sourceOptions.find((item) => item.value === event.target.value);
                        onBindingChange(index, { asset_ref: event.target.value, display_name: selected?.label || "", fields: "" });
                      }}
                      className="mt-1 h-10 w-full rounded-xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    >
                      <option value="">选择已登记数据资产</option>
                      {sourceOptions.map((source) => <option key={source.value} value={source.value}>{source.label}</option>)}
                    </select>
                  </label>
                  <label className="block">
                    <span className="text-[11px] font-semibold text-gray-400">字段</span>
                    <div className="mt-1 flex items-center gap-2">
                      <select
                        value={selectedFields[0] || ""}
                        disabled={!binding.asset_ref || fieldsLoading || fieldOptions.length === 0}
                        onChange={(event) => onBindingChange(index, { fields: event.currentTarget.value })}
                        className="h-10 min-w-0 flex-1 rounded-xl border border-black/[0.08] bg-white px-3 text-sm outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                      >
                        <option value="">{fieldsLoading ? "正在读取字段..." : binding.asset_ref ? "选择字段" : "请先选择数据资产"}</option>
                        {fieldOptions.map((field) => <option key={field} value={field}>{currentOnlyFields.includes(field) ? `当前配置：${field}` : field}</option>)}
                      </select>
                      <button
                        type="button"
                        onClick={() => onReloadFields(binding.asset_ref)}
                        disabled={!binding.asset_ref || fieldsLoading}
                        className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-black/[0.08] text-gray-500 transition hover:border-[#002fa7]/30 hover:bg-[#002fa7]/[0.04] hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
                        title="重新读取字段"
                        aria-label="重新读取字段"
                      >
                        <RefreshCw className={`h-4 w-4 ${fieldsLoading ? "animate-spin" : ""}`} />
                      </button>
                    </div>
                  </label>
                </div>
                {!fieldsLoading && binding.asset_ref && fieldOptions.length === 0 ? <p className="mt-2 text-[11px] text-amber-600">未读取到字段。表格资产需要先生成 Profile。</p> : null}
              </div>
            );
          })}
        </div>
      )}
      {mode === "derived" ? (
        <label className="block text-sm font-semibold text-gray-700">
          推导规则 / SQL Hint
          <textarea value={derivedExpression} onChange={(event) => onDerivedExpressionChange(event.target.value)} className="mt-2 min-h-20 w-full rounded-xl border border-black/[0.08] bg-white px-3 py-2 font-mono text-xs outline-none focus:border-[#002fa7]/40" placeholder="CASE WHEN price &lt; 10 THEN '5-10万元' ... END" />
        </label>
      ) : null}
      {mode === "calendar_lookup" ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-sm font-semibold text-gray-700">周起始日
            <select value={weekStartDay} onChange={(event) => onWeekStartDayChange(event.target.value)} className="mt-2 h-10 w-full rounded-xl border border-black/[0.08] bg-white px-3 text-sm outline-none"><option value="monday">周一</option><option value="sunday">周日</option></select>
          </label>
          <label className="block text-sm font-semibold text-gray-700">时区
            <input value={timezone} onChange={(event) => onTimezoneChange(event.target.value)} className="mt-2 h-10 w-full rounded-xl border border-black/[0.08] bg-white px-3 text-sm outline-none" />
          </label>
        </div>
      ) : null}
    </section>
  );
}

function DimensionDefinitionEditorModal({
  asset,
  tableAssets,
  databaseSources,
  busy,
  initialTab = "settings",
  onClose,
  onSave,
  onSaveMarkdown,
}: {
  asset: SemanticAssetDetail;
  tableAssets: TableAsset[];
  databaseSources: KnowledgeDatabaseSource[];
  busy: boolean;
  initialTab?: "settings" | "matching" | "markdown";
  onClose: () => void;
  onSave: (payload: SemanticDimensionUpdatePayload) => void;
  onSaveMarkdown: (content: string) => void;
}) {
  const existing = (asset.frontmatter?.resolution as Record<string, unknown> | undefined) || {};
  const initialMode = (asset.resolution_mode || existing.mode || "source_field") as DimensionDefinition["mode"];
  const initialBindings = Array.isArray(existing.bindings)
    ? existing.bindings.map((binding) => {
        const raw = binding as Record<string, unknown>;
        const fields = raw.fields && typeof raw.fields === "object" ? Object.values(raw.fields as Record<string, unknown>).map(String).join(", ") : "";
        return { asset_ref: String(raw.asset_ref || ""), display_name: String(raw.display_name || ""), fields };
      })
    : [];
  const [mode, setMode] = useState<DimensionDefinition["mode"]>(initialMode);
  const [bindings, setBindings] = useState<Array<{ asset_ref: string; display_name: string; fields: string }>>(
    initialBindings.length ? initialBindings : [{ asset_ref: "", display_name: "", fields: "" }]
  );
  const [expression, setExpression] = useState(String(existing.expression || ""));
  const canonical = existing.canonical as Record<string, unknown> | undefined;
  const [canonicalFields, setCanonicalFields] = useState(Array.isArray(canonical?.fields) ? canonical.fields.map(String).join(", ") : "");
  const [referencePath, setReferencePath] = useState(String(existing.reference_path || ""));
  const [weekStartDay, setWeekStartDay] = useState(String(existing.week_start_day || "monday"));
  const [timezone, setTimezone] = useState(String(existing.timezone || "Asia/Shanghai"));
  const [name, setName] = useState(asset.name);
  const [description, setDescription] = useState(asset.description || "");
  const [aliases, setAliases] = useState((asset.aliases || []).join(", "));
  const [tags, setTags] = useState((asset.tags || []).join(", "));
  const [version, setVersion] = useState(String(asset.frontmatter?.version || "0.1.0"));
  const [tab, setTab] = useState<"settings" | "matching" | "markdown">(initialTab);
  const [markdown, setMarkdown] = useState("");
  const [markdownLoading, setMarkdownLoading] = useState(false);
  useEffect(() => { setTab(initialTab); }, [asset.id, initialTab]);
  useEffect(() => {
    if (tab !== "markdown" || markdown) return;
    setMarkdownLoading(true);
    void readFile(asset.path)
      .then(setMarkdown)
      .finally(() => setMarkdownLoading(false));
  }, [asset.path, markdown, tab]);
  const sourceOptions = [
    ...tableAssets.map((item) => ({ value: `table_asset:${item.asset_id}`, label: `${item.file_name}${item.sheet_name ? ` · ${item.sheet_name}` : ""}` })),
    ...databaseSources.flatMap((source) => source.selected_tables.length
      ? source.selected_tables.map((table) => ({ value: `${source.id}.${table}`, label: `${source.name} · ${table}` }))
      : [{ value: `database_source:${source.id}`, label: `${source.name} · 未选择表` }]),
  ];
  const { columnsByAsset, loadingByAsset, reloadColumns } = useDimensionBindingColumns(
    mode === "entity_lookup" ? [] : bindings,
    tableAssets,
    databaseSources
  );
  const updateBinding = (index: number, changes: Partial<{ asset_ref: string; display_name: string; fields: string }>) => {
    setBindings((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, ...changes } : item));
  };
  const buildDefinition = (): DimensionDefinition => {
    const activeBindings = mode === "entity_lookup" ? bindings : bindings.slice(0, 1);
    const normalizedBindings = activeBindings.filter((binding) => binding.asset_ref || binding.fields.trim()).map((binding) => {
      const fields = splitTokenList(binding.fields);
      const mapped = mode === "source_field" || mode === "derived"
        ? { value: fields[0] || "" }
        : mode === "calendar_lookup"
          ? { date: fields[0] || "" }
          : Object.fromEntries(fields.map((field, index) => [`key_${index + 1}`, field]));
      return { asset_ref: binding.asset_ref, display_name: binding.display_name, fields: mapped };
    });
    const definition: DimensionDefinition = { mode, bindings: normalizedBindings };
    if (mode === "derived") {
      definition.source_fields = splitTokenList(activeBindings.flatMap((binding) => splitTokenList(binding.fields)).join(","));
      definition.expression = expression;
    }
    if (mode === "entity_lookup") {
      definition.canonical = { key: "entity_key", fields: splitTokenList(canonicalFields) };
      definition.reference_path = referencePath;
    }
    if (mode === "calendar_lookup") {
      definition.date_field = splitTokenList(bindings[0]?.fields || "")[0] || "";
      definition.week_start_day = weekStartDay;
      definition.timezone = timezone;
    }
    return definition;
  };

  return (
    <div className="fixed inset-0 z-[140] flex items-start justify-center bg-black/40 px-4 py-[5vh] backdrop-blur-sm">
      <div className="flex max-h-[90vh] w-[96vw] max-w-5xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-950">编辑维度</h3>
            <p className="mt-1 text-sm text-gray-500">维度以 dimension.md 保存；结构化字段写入 frontmatter，正文用于业务说明和 LLM 理解。</p>
            <p className="mt-2 font-mono text-xs text-gray-400">{asset.path}</p>
          </div>
          <button type="button" onClick={onClose} disabled={busy} className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900" aria-label="关闭"><X className="h-5 w-5" /></button>
        </div>
        <div className="min-h-0 overflow-auto px-6 py-5">
          <div className="inline-grid grid-cols-3 rounded-2xl bg-gray-100 p-1">
            {[
              { value: "settings", label: "结构化录入" },
              { value: "matching", label: "匹配管理" },
              { value: "markdown", label: "原始 Markdown" },
            ].map((item) => (
              <button
                key={item.value}
                type="button"
                onClick={() => setTab(item.value as "settings" | "matching" | "markdown")}
                className={`h-10 rounded-xl px-4 text-sm font-semibold transition ${tab === item.value ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500 hover:text-gray-800"}`}
              >
                {item.label}
              </button>
            ))}
          </div>

          <div className="mt-5">
            {tab === "settings" ? (
              <div className="space-y-5">
                <section className="rounded-3xl border border-black/[0.06] p-4">
                  <h4 className="text-sm font-semibold text-gray-950">基础信息</h4>
                  <div className="mt-4 grid gap-4 md:grid-cols-2">
                    <LabeledInput label="名称" value={name} onChange={setName} placeholder="上市时间" />
                    <LabeledInput label="版本" value={version} onChange={setVersion} placeholder="0.1.0" />
                    <LabeledTextarea label="描述" value={description} onChange={setDescription} placeholder="说明该维度的业务含义、使用边界和取值口径。" />
                    <div className="grid gap-4 sm:grid-cols-2">
                      <LabeledInput label="别名" value={aliases} onChange={setAliases} placeholder="多个别名用逗号分隔" />
                      <LabeledInput label="标签" value={tags} onChange={setTags} placeholder="多个标签用逗号分隔" />
                    </div>
                  </div>
                </section>

                <section className="rounded-3xl border border-black/[0.06] p-4">
                  <h4 className="text-sm font-semibold text-gray-950">维度取值方式</h4>
                  <p className="mt-1 text-xs text-gray-500">选择值从哪里来；复杂的多表清洗由“构建语义维度”技能完成，再把结果绑定到这里。</p>
                  <div className="mt-4">
                    <DimensionDefinitionFields
                      embedded
                      mode={mode}
                      bindings={bindings}
                      sourceOptions={sourceOptions}
                      fieldOptionsByAsset={columnsByAsset}
                      fieldOptionsLoading={loadingByAsset}
                      onReloadFields={reloadColumns}
                      derivedExpression={expression}
                      canonicalFields={canonicalFields}
                      referencePath={referencePath}
                      weekStartDay={weekStartDay}
                      timezone={timezone}
                      onModeChange={setMode}
                      onBindingChange={updateBinding}
                      onAddBinding={() => setBindings((current) => [...current, { asset_ref: "", display_name: "", fields: "" }])}
                      onRemoveBinding={(index) => setBindings((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                      onDerivedExpressionChange={setExpression}
                      onCanonicalFieldsChange={setCanonicalFields}
                      onReferencePathChange={setReferencePath}
                      onWeekStartDayChange={setWeekStartDay}
                      onTimezoneChange={setTimezone}
                    />
                  </div>
                </section>
              </div>
            ) : tab === "matching" ? (
              <DimensionMatchingManager dimensionId={asset.id.replace(/^dimension:/, "")} />
            ) : markdownLoading ? (
              <div className="flex min-h-[420px] items-center justify-center rounded-3xl border border-dashed border-black/[0.08] text-sm text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在读取 dimension.md…</div>
            ) : (
              <section className="rounded-3xl border border-black/[0.06] p-4">
                <h4 className="text-sm font-semibold text-gray-950">原始 dimension.md</h4>
                <p className="mt-1 text-xs text-gray-500">直接编辑完整 Markdown。保存后后端会重新解析 frontmatter 并刷新语义资产 registry。</p>
                <textarea value={markdown} onChange={(event) => setMarkdown(event.target.value)} spellCheck={false} className="mt-4 min-h-[520px] w-full resize-y rounded-2xl border border-black/[0.08] bg-gray-950 p-4 font-mono text-xs leading-5 text-gray-100 outline-none focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]" />
              </section>
            )}
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4">
          <button type="button" onClick={onClose} disabled={busy} className="h-10 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700">{tab === "matching" ? "关闭" : "取消"}</button>
          {tab !== "matching" ? (
            <button type="button" onClick={() => tab === "settings" ? onSave({ name, description, aliases: splitTokenList(aliases), tags: splitTokenList(tags), version: version.trim() || "0.1.0", dimension_definition: buildDefinition() }) : onSaveMarkdown(markdown)} disabled={busy || (tab === "settings" && !name.trim()) || (tab === "markdown" && markdownLoading)} className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:opacity-45">
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />} {tab === "settings" ? "保存设置" : "保存 Markdown"}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function EntityTargetPicker({ value, options, onChange, open, onOpenChange }: { value: string; options: Array<{ entity_key: string; label: string }>; onChange: (value: string) => void; open: boolean; onOpenChange: (open: boolean) => void }) {
  const selected = options.find((item) => item.entity_key === value);
  const [query, setQuery] = useState(selected?.label || "");
  useEffect(() => { setQuery(options.find((item) => item.entity_key === value)?.label || ""); }, [options, value]);
  const matches = options.filter((item) => `${item.label} ${item.entity_key}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())).slice(0, 12);
  return <div className="relative">
    <input value={query} onFocus={() => onOpenChange(true)} onBlur={() => onOpenChange(false)} onChange={(event) => { setQuery(event.target.value); onOpenChange(true); }} placeholder="搜索规范实体" className="h-9 w-full rounded-lg border border-black/[0.1] bg-white px-2.5 text-xs text-slate-700 outline-none focus:border-[#002fa7]" />
    {open ? <div className="absolute z-20 mt-1 max-h-56 w-full overflow-auto rounded-lg border border-black/[0.1] bg-white py-1 shadow-lg">
      {matches.map((item) => <button key={item.entity_key} type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => { onChange(item.entity_key); setQuery(item.label); onOpenChange(false); }} className="block w-full px-3 py-2 text-left text-xs text-slate-700 hover:bg-[#002fa7]/[0.05]"><span className="font-medium">{item.label}</span><span className="ml-2 font-mono text-[10px] text-slate-400">{item.entity_key}</span></button>)}
      {!matches.length ? <p className="px-3 py-2 text-xs text-slate-400">未找到规范实体</p> : null}
    </div> : null}
  </div>;
}

function MatchingPagination({ page, count, onChange }: { page: number; count: number; onChange: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(count / MATCHING_PAGE_SIZE));
  const start = count ? page * MATCHING_PAGE_SIZE + 1 : 0;
  const end = Math.min((page + 1) * MATCHING_PAGE_SIZE, count);
  return <div className="flex min-h-12 items-center justify-between gap-3 border-t border-black/[0.06] bg-white px-4 py-2"><p className="text-xs text-slate-400">显示 {start}-{end}，共 {count} 条</p>{pages > 1 ? <div className="flex items-center gap-2"><button type="button" aria-label="上一页" disabled={page === 0} onClick={() => onChange(page - 1)} className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-black/[0.1] text-slate-600 disabled:opacity-35"><ChevronLeft className="h-4 w-4" /></button><span className="min-w-16 text-center text-xs text-slate-500">{page + 1} / {pages}</span><button type="button" aria-label="下一页" disabled={page + 1 >= pages} onClick={() => onChange(page + 1)} className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-black/[0.1] text-slate-600 disabled:opacity-35"><ChevronRight className="h-4 w-4" /></button></div> : null}</div>;
}

function DimensionMatchingManager({ dimensionId }: { dimensionId: string }) {
  const [overview, setOverview] = useState<SemanticDimensionMatchingOverview | null>(null);
  const [sourceView, setSourceView] = useState<SemanticDimensionMatchingView | null>(null);
  const [baselineChange, setBaselineChange] = useState<SemanticDimensionBaselineChange | null>(null);
  const [mode, setMode] = useState<"overview" | "source">("overview");
  const [loading, setLoading] = useState(true);
  const [savingKey, setSavingKey] = useState("");
  const [publishing, setPublishing] = useState(false);
  const [error, setError] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [overviewQuery, setOverviewQuery] = useState("");
  const [sourceQuery, setSourceQuery] = useState("");
  const [overviewPage, setOverviewPage] = useState(0);
  const [sourcePage, setSourcePage] = useState(0);
  const [openTargetKey, setOpenTargetKey] = useState("");
  const [targetByRow, setTargetByRow] = useState<Record<string, string>>({});
  const [pendingEntityRemoval, setPendingEntityRemoval] = useState<{ entityKey: string; label: string } | null>(null);
  const [pendingBaselineRemoval, setPendingBaselineRemoval] = useState(false);
  const requestSequence = useRef(0);

  const load = useCallback(async () => {
    const requestId = ++requestSequence.current;
    setLoading(true); setError("");
    try {
      const overviewRequest = getSemanticDimensionMatchingOverview(dimensionId, { query: overviewQuery, offset: overviewPage * MATCHING_PAGE_SIZE, limit: MATCHING_PAGE_SIZE });
      const baselineRequest = getSemanticDimensionBaselineChange(dimensionId);
      const sourceRequest = sourceFilter
        ? getSemanticDimensionMatching(dimensionId, { status: statusFilter, sourceRef: sourceFilter, query: sourceQuery, offset: sourcePage * MATCHING_PAGE_SIZE, limit: MATCHING_PAGE_SIZE })
        : Promise.resolve(null);
      const [nextOverview, nextBaselineChange, nextSourceView] = await Promise.all([overviewRequest, baselineRequest, sourceRequest]);
      if (requestId !== requestSequence.current) return;
      setOverview(nextOverview);
      setBaselineChange(nextBaselineChange);
      setSourceView(nextSourceView);
    } catch (caught) { if (requestId === requestSequence.current) setError(errorMessage(caught)); }
    finally { if (requestId === requestSequence.current) setLoading(false); }
  }, [dimensionId, overviewPage, overviewQuery, sourceFilter, sourcePage, sourceQuery, statusFilter]);
  useEffect(() => { void load(); }, [load]);

  const rowKey = (row: SemanticDimensionMatchRow, index: number) => `${index}:${row.binding?.source_ref || "source"}:${JSON.stringify(row.binding?.key_fields || {})}`;
  const statusLabel = (status?: string) => ({ auto_matched: "自动匹配", manual_override: "人工覆盖", canonical_only: "仅规范实体", unmatched: "未匹配", candidate: "待审核", manual_excluded: "已排除", inactive: "已停用" }[status || ""] || status || "未知");
  const statusTone = (status?: string) => status === "auto_matched" || status === "manual_override" ? "bg-emerald-50 text-emerald-700" : status === "unmatched" || status === "candidate" ? "bg-amber-50 text-amber-700" : "bg-slate-100 text-slate-600";
  const changeOverride = async (row: SemanticDimensionMatchRow, index: number, action: "bind" | "exclude") => {
    const binding = row.binding; if (!binding?.source_ref || !binding.key_fields) return;
    const key = rowKey(row, index); const target = targetByRow[key] || row.entity_key || "";
    if (action === "bind" && !target) { setError("请先选择要绑定的规范实体。"); return; }
    setSavingKey(key); setError("");
    try {
      await saveSemanticDimensionOverride(dimensionId, { source_ref: binding.source_ref, source_id: binding.source_id, scope: "source_id", source_key: binding.key_fields, action, target_entity_key: target, reason: action === "bind" ? "用户在匹配管理中确认绑定。" : "用户在匹配管理中明确不关联。", source_name: binding.source_name, source_kind: binding.source_kind, table_or_sheet: binding.table_or_sheet });
      await load();
    } catch (caught) { setError(errorMessage(caught)); } finally { setSavingKey(""); }
  };
  const revoke = async (overrideId: string) => { setSavingKey(overrideId); setError(""); try { await deleteSemanticDimensionOverride(dimensionId, overrideId); await load(); } catch (caught) { setError(errorMessage(caught)); } finally { setSavingKey(""); } };
  const changeLifecycle = async (entityKey: string, action: "active" | "inactive" | "remove") => {
    setSavingKey(`entity:${entityKey}`); setError("");
    try {
      await saveSemanticDimensionEntityLifecycle(dimensionId, {
        entity_key: entityKey,
        action,
        reason: action === "inactive" ? "用户在匹配管理中主动停用规范实体。" : action === "remove" ? "用户在匹配管理中确认移除规范实体。" : "用户在匹配管理中恢复规范实体。",
      });
      await load();
    } catch (caught) { setError(errorMessage(caught)); } finally { setSavingKey(""); }
  };
  const resolveBaselineChange = async (action: "inactive" | "remove" | "cancel") => {
    if (!baselineChange) return;
    setSavingKey(`baseline:${action}`); setError("");
    try {
      await resolveSemanticDimensionBaselineChange(baselineChange.job.id, action);
      await load();
    } catch (caught) { setError(errorMessage(caught)); } finally { setSavingKey(""); }
  };
  const publish = async () => { setPublishing(true); setError(""); try { await publishSemanticDimensionMatching(dimensionId); await load(); } catch (caught) { setError(errorMessage(caught)); } finally { setPublishing(false); } };

  if (loading && !overview) return <div className="flex min-h-[420px] items-center justify-center rounded-3xl border border-dashed border-black/[0.08] text-sm text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在读取匹配结果…</div>;
  if (!overview) return <div className="rounded-3xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-800">{error || "尚无可编辑的已发布 Crosswalk。请先通过构建任务发布一个版本。"}</div>;
  const source = overview.sources.find((item) => item.id === sourceFilter);

  return <div className="space-y-4">
    <section className="rounded-3xl border border-black/[0.06] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><h4 className="text-sm font-semibold text-gray-950">匹配管理</h4><p className="mt-1 text-xs leading-5 text-gray-500">编辑先保存为草稿；仅在发布后才更新运行中的 active_crosswalk.json 并生成版本快照。</p></div><div className="flex items-center gap-2"><span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-semibold text-[#002fa7]">{overview.version}</span><button type="button" onClick={() => void load()} disabled={loading} className="inline-flex h-8 items-center gap-1.5 rounded-xl border border-black/[0.08] px-2.5 text-xs font-semibold text-gray-600 hover:bg-slate-50"><RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />刷新</button></div></div>
      <div className="mt-4 grid gap-2 sm:grid-cols-3"><MetricCard icon={Layers3} title="规范实体" value={overview.summary.canonical_entities} tone="blue" compact /><MetricCard icon={CheckCircle2} title="人工匹配规则" value={Number(overview.summary.manual_overrides || 0) + Number(overview.summary.manual_entity_overrides || 0)} tone="green" compact /><MetricCard icon={Database} title="数据来源" value={overview.summary.sources} tone="blue" compact /></div>
      {overview.has_unpublished_changes ? <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-3 py-2"><p className="text-xs font-medium text-amber-800">存在未发布的人工匹配草稿。运行中的 Agent 仍使用 {overview.version}。</p><button type="button" onClick={() => void publish()} disabled={publishing} className="inline-flex h-8 items-center gap-1.5 rounded-xl bg-[#002fa7] px-3 text-xs font-semibold text-white disabled:opacity-50">{publishing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}发布匹配版本</button></div> : null}
      {baselineChange ? <div className="mt-3 rounded-2xl border border-amber-200 bg-amber-50 p-3"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold text-amber-900">发现 {baselineChange.baseline_delta.removed?.length || 0} 条规范基准移除</p><p className="mt-1 text-xs leading-5 text-amber-800">构建产物仍在 staging，尚未影响 Agent。请在这里决定如何处理。</p></div><div className="flex flex-wrap gap-2"><button type="button" onClick={() => void resolveBaselineChange("inactive")} disabled={savingKey.startsWith("baseline:")} className="h-8 rounded-lg border border-amber-300 bg-white px-2.5 text-xs font-semibold text-amber-800 disabled:opacity-50">保留为停用</button><button type="button" onClick={() => setPendingBaselineRemoval(true)} disabled={savingKey.startsWith("baseline:")} className="h-8 rounded-lg border border-rose-200 bg-white px-2.5 text-xs font-semibold text-rose-600 disabled:opacity-50">移除</button><button type="button" onClick={() => void resolveBaselineChange("cancel")} disabled={savingKey.startsWith("baseline:")} className="h-8 rounded-lg border border-black/[0.1] bg-white px-2.5 text-xs font-semibold text-slate-600 disabled:opacity-50">取消本次变更</button></div></div>{baselineChange.baseline_delta.removed?.length ? <div className="mt-2 flex flex-wrap gap-1.5">{baselineChange.baseline_delta.removed.slice(0, 12).map((item) => <span key={item.entity_key} className="rounded bg-white px-2 py-1 font-mono text-[11px] text-amber-800">{item.label || item.entity_key}</span>)}{baselineChange.baseline_delta.removed.length > 12 ? <span className="px-1 py-1 text-[11px] text-amber-700">另有 {baselineChange.baseline_delta.removed.length - 12} 条</span> : null}</div> : null}</div> : null}
    </section>
    <div className="inline-flex rounded-2xl bg-slate-100 p-1"><button type="button" onClick={() => setMode("overview")} className={`h-9 rounded-xl px-4 text-sm font-semibold ${mode === "overview" ? "bg-white text-[#002fa7] shadow-sm" : "text-slate-500"}`}>全局总览</button><button type="button" onClick={() => setMode("source")} className={`h-9 rounded-xl px-4 text-sm font-semibold ${mode === "source" ? "bg-white text-[#002fa7] shadow-sm" : "text-slate-500"}`}>按来源编辑</button></div>
    {mode === "overview" ? <label className="relative block max-w-xl"><Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" /><input value={overviewQuery} onChange={(event) => { setOverviewQuery(event.target.value); setOverviewPage(0); }} placeholder="搜索规范实体、entity_key 或任一来源匹配键" className="h-10 w-full rounded-xl border border-black/[0.1] bg-white pl-9 pr-3 text-sm text-slate-800 outline-none focus:border-[#002fa7]" /></label> : null}
    {error ? <p className="rounded-xl bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p> : null}
    {mode === "overview" ? <section className="overflow-hidden rounded-3xl border border-black/[0.06]"><div className="max-h-[480px] overflow-auto"><table className="w-full min-w-[1040px] text-left text-sm"><thead className="sticky top-0 bg-slate-50 text-xs text-slate-500"><tr><th className="min-w-64 px-4 py-3 font-semibold">规范实体</th>{overview.sources.map((item) => <th key={item.id} className="min-w-64 px-4 py-3 font-semibold">{item.name}<span className="mt-0.5 block font-normal text-slate-400">{item.identity_fields?.join(" + ")}</span></th>)}<th className="min-w-40 px-4 py-3 font-semibold">维护</th></tr></thead><tbody className="divide-y divide-black/[0.05] bg-white">{overview.rows.map((row) => { const inactive = row.status === "inactive"; const busy = savingKey === `entity:${row.entity_key}`; return <tr key={row.entity_key}><td className="px-4 py-3"><p className="font-semibold text-slate-900">{row.canonical_label}</p><p className="mt-1 font-mono text-xs text-slate-400">{row.entity_key}</p></td>{overview.sources.map((item) => { const cells = row.source_cells[item.id] || []; return <td key={item.id} className="px-4 py-3">{cells.length ? cells.map((cell, index) => <p key={`${cell.source_ref}:${index}`} className="mb-1 text-xs text-slate-600">{Object.values(cell.source_key).join(" / ")}{cell.manual ? <span className="ml-1.5 rounded bg-[#002fa7]/10 px-1.5 py-0.5 text-[10px] font-semibold text-[#002fa7]">人工</span> : null}</p>) : <span className="text-xs text-slate-300">-</span>}</td>; })}<td className="px-4 py-3"><div className="flex flex-wrap gap-2">{inactive ? <button type="button" disabled={busy} onClick={() => void changeLifecycle(row.entity_key, "active")} className="h-8 rounded-lg border border-emerald-200 px-2.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 disabled:opacity-50">恢复</button> : <button type="button" disabled={busy} onClick={() => void changeLifecycle(row.entity_key, "inactive")} className="h-8 rounded-lg border border-amber-200 px-2.5 text-xs font-semibold text-amber-700 hover:bg-amber-50 disabled:opacity-50">停用</button>}<button type="button" disabled={busy} onClick={() => setPendingEntityRemoval({ entityKey: row.entity_key, label: row.canonical_label })} className="h-8 rounded-lg border border-rose-200 px-2.5 text-xs font-semibold text-rose-600 hover:bg-rose-50 disabled:opacity-50">移除</button></div></td></tr>; })}{loading ? <tr><td colSpan={Math.max(1, overview.sources.length + 2)} className="px-4 py-12 text-center text-sm text-slate-400"><span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin text-[#002fa7]" />正在更新匹配结果…</span></td></tr> : !overview.rows.length ? <tr><td colSpan={Math.max(1, overview.sources.length + 2)} className="px-4 py-12 text-center text-sm text-slate-400">暂无规范实体。</td></tr> : null}</tbody></table></div><MatchingPagination page={overviewPage} count={overview.count} onChange={setOverviewPage} /></section> : <>
      <section className="rounded-3xl border border-black/[0.06] p-4"><div className="flex flex-wrap items-end gap-3"><label className="min-w-[260px] flex-1 text-xs font-semibold text-gray-600">编辑来源<select value={sourceFilter} onChange={(event) => { setSourceFilter(event.target.value); setStatusFilter(""); setSourceQuery(""); setSourcePage(0); }} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] bg-white px-3 text-sm font-normal text-gray-800 outline-none focus:border-[#002fa7]"><option value="">选择一个来源</option>{overview.sources.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.identity_fields?.join(" + ")}</option>)}</select></label>{sourceView ? <label className="min-w-[180px] flex-1 text-xs font-semibold text-gray-600">状态<select value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); setSourcePage(0); }} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] bg-white px-3 text-sm font-normal text-gray-800 outline-none focus:border-[#002fa7]"><option value="">全部状态</option>{Object.keys(sourceView.summary.status_counts).map((status) => <option key={status} value={status}>{statusLabel(status)} ({sourceView.summary.status_counts[status]})</option>)}</select></label> : null}<label className="relative min-w-[280px] flex-[1.4]"><span className="mb-1 block text-xs font-semibold text-gray-600">搜索来源键</span><Search className="pointer-events-none absolute bottom-3 left-3 h-4 w-4 text-slate-400" /><input value={sourceQuery} onChange={(event) => { setSourceQuery(event.target.value); setSourcePage(0); }} disabled={!sourceFilter} placeholder="品牌、车系或当前规范实体" className="h-10 w-full rounded-xl border border-black/[0.1] bg-white pl-9 pr-3 text-sm text-slate-800 outline-none focus:border-[#002fa7] disabled:bg-slate-50" /></label></div>{source ? <p className="mt-3 text-xs text-slate-500">正在编辑「{source.name}」的来源键。选择目标后保存为草稿，发布前不会改变 Agent 查询结果。</p> : null}</section>
      {sourceView ? <section className="overflow-hidden rounded-3xl border border-black/[0.06]"><div className="max-h-[480px] overflow-auto"><table className="w-full min-w-[1120px] table-fixed text-left text-sm"><colgroup><col className="w-[28%]" /><col className="w-[30%]" /><col className="w-[12%]" /><col className="w-[30%]" /></colgroup><thead className="sticky top-0 bg-slate-50 text-xs text-slate-500"><tr><th className="px-4 py-3 font-semibold">来源匹配键</th><th className="px-4 py-3 font-semibold">当前规范实体</th><th className="px-4 py-3 font-semibold">状态</th><th className="px-4 py-3 font-semibold">人工改绑</th></tr></thead><tbody className="divide-y divide-black/[0.05] bg-white">{sourceView.rows.map((row, index) => { const key = rowKey(row, index); const binding = row.binding; const target = targetByRow[key] ?? row.entity_key ?? ""; return <tr key={key} className="align-top"><td className="px-4 py-3"><p className="font-medium text-slate-700">{binding?.source_name || source?.name || "-"}</p><p className="mt-1 text-xs text-slate-500">{binding?.key_fields ? Object.entries(binding.key_fields).map(([field, value]) => `${field}: ${String(value)}`).join(" · ") : "-"}</p></td><td className="px-4 py-3"><p className="font-semibold text-slate-900">{row.canonical_label || "未归属"}</p>{row.entity_key ? <p className="mt-1 font-mono text-xs text-slate-400">{row.entity_key}</p> : null}</td><td className="px-4 py-3"><span className={`inline-flex whitespace-nowrap rounded-full px-2 py-1 text-xs font-semibold ${statusTone(row.status)}`}>{statusLabel(row.status)}</span></td><td className="px-4 py-3"><div className="flex min-w-0 items-start gap-2"><div className="min-w-0 flex-1"><EntityTargetPicker value={target} options={sourceView.entity_options} open={openTargetKey === key} onOpenChange={(open) => setOpenTargetKey(open ? key : "")} onChange={(value) => setTargetByRow((current) => ({ ...current, [key]: value }))} /></div>{row.manual && row.override_id ? <button type="button" onClick={() => void revoke(row.override_id!)} disabled={savingKey === row.override_id} className="h-9 shrink-0 rounded-lg border border-black/[0.1] px-2.5 text-xs font-semibold text-slate-600 hover:bg-slate-50">撤销</button> : <><button type="button" onClick={() => void changeOverride(row, index, "bind")} disabled={savingKey === key} className="h-9 shrink-0 rounded-lg bg-[#002fa7] px-2.5 text-xs font-semibold text-white disabled:opacity-50">保存</button><button type="button" onClick={() => void changeOverride(row, index, "exclude")} disabled={savingKey === key} className="h-9 shrink-0 rounded-lg border border-rose-200 px-2.5 text-xs font-semibold text-rose-600 hover:bg-rose-50">不关联</button></>}</div></td></tr>; })}{loading ? <tr><td colSpan={4} className="px-4 py-12 text-center text-sm text-slate-400"><span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin text-[#002fa7]" />正在更新来源匹配结果…</span></td></tr> : !sourceView.rows.length ? <tr><td colSpan={4} className="px-4 py-12 text-center text-sm text-slate-400">这个来源没有符合当前条件的记录。</td></tr> : null}</tbody></table></div><MatchingPagination page={sourcePage} count={sourceView.count} onChange={setSourcePage} /></section> : sourceFilter && loading ? <div className="flex min-h-[280px] items-center justify-center rounded-3xl border border-dashed border-black/[0.08] text-sm text-slate-400"><Loader2 className="mr-2 h-4 w-4 animate-spin text-[#002fa7]" />正在加载来源匹配结果…</div> : <div className="rounded-3xl border border-dashed border-black/[0.08] px-5 py-14 text-center text-sm text-slate-400">先选择一个来源，再人工审核其来源键到规范实体的映射。</div>}</>}
    {pendingEntityRemoval ? <div className="fixed inset-0 z-[180] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm"><div className="w-full max-w-md rounded-[28px] bg-white p-6 shadow-2xl ring-1 ring-black/[0.08]"><h4 className="text-lg font-semibold text-gray-950">移除规范实体</h4><p className="mt-3 text-sm leading-6 text-slate-600">确定将「{pendingEntityRemoval.label}」从下一个活跃版本中移除吗？本次操作先保存为草稿，发布前不会影响 Agent；历史版本仍可追溯。</p><p className="mt-3 rounded-xl bg-slate-50 px-3 py-2 font-mono text-xs text-slate-500">{pendingEntityRemoval.entityKey}</p><div className="mt-6 flex justify-end gap-2"><button type="button" onClick={() => setPendingEntityRemoval(null)} className="h-10 rounded-xl border border-black/[0.08] px-4 text-sm font-semibold text-slate-600">取消</button><button type="button" onClick={() => { const target = pendingEntityRemoval; setPendingEntityRemoval(null); void changeLifecycle(target.entityKey, "remove"); }} className="h-10 rounded-xl bg-rose-600 px-4 text-sm font-semibold text-white">移除</button></div></div></div> : null}
    {pendingBaselineRemoval && baselineChange ? <div className="fixed inset-0 z-[180] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm"><div className="w-full max-w-md rounded-[28px] bg-white p-6 shadow-2xl ring-1 ring-black/[0.08]"><h4 className="text-lg font-semibold text-gray-950">移除规范基准</h4><p className="mt-3 text-sm leading-6 text-slate-600">确定从下一个活跃版本移除 {baselineChange.baseline_delta.removed?.length || 0} 条已不在当前基准中的规范实体吗？此操作会先形成草稿，发布前不会影响 Agent，历史版本仍可追溯。</p><div className="mt-6 flex justify-end gap-2"><button type="button" onClick={() => setPendingBaselineRemoval(false)} className="h-10 rounded-xl border border-black/[0.08] px-4 text-sm font-semibold text-slate-600">取消</button><button type="button" onClick={() => { setPendingBaselineRemoval(false); void resolveBaselineChange("remove"); }} className="h-10 rounded-xl bg-rose-600 px-4 text-sm font-semibold text-white">移除</button></div></div></div> : null}
  </div>;
}

function SemanticAssetImportModal({
  busy,
  onClose,
  onImport,
}: {
  busy: boolean;
  onClose: () => void;
  onImport: (files: File[]) => void;
}) {
  const [mode, setMode] = useState<"zip" | "folder">("zip");
  const [files, setFiles] = useState<File[]>([]);

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="w-full max-w-xl overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-950">导入语义资产</h3>
            <p className="mt-1 text-sm text-gray-500">支持 ZIP 或文件夹，至少包含一个 measure.md、grain.md 或 dimension.md。</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900">
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="space-y-4 px-6 py-5">
          <div className="inline-flex rounded-2xl bg-gray-100 p-1">
            {(["zip", "folder"] as const).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => {
                  setMode(item);
                  setFiles([]);
                }}
                className={`h-9 rounded-xl px-4 text-sm font-semibold transition ${
                  mode === item ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500"
                }`}
              >
                {item === "zip" ? "ZIP" : "文件夹"}
              </button>
            ))}
          </div>
          <label className="flex min-h-40 cursor-pointer flex-col items-center justify-center rounded-3xl border border-dashed border-black/[0.12] bg-[#f8fafc] px-5 py-8 text-center transition hover:border-[#002fa7]/30 hover:bg-[#002fa7]/[0.025]">
            <Upload className="h-8 w-8 text-[#002fa7]" />
            <span className="mt-3 text-sm font-semibold text-gray-800">
              {files.length ? `已选择 ${files.length} 个文件` : mode === "zip" ? "选择 ZIP 文件" : "选择语义资产文件夹"}
            </span>
            <span className="mt-1 text-xs text-gray-400">
              后端会归一化到 backend/semantic-assets
            </span>
            <input
              type="file"
              className="hidden"
              accept={mode === "zip" ? ".zip" : undefined}
              multiple={mode === "folder"}
              {...(mode === "folder" ? { webkitdirectory: "" } : {})}
              onChange={(event) => setFiles(Array.from(event.target.files || []))}
            />
          </label>
          {files.length ? (
            <div className="max-h-28 overflow-auto rounded-2xl bg-gray-50 p-3 text-xs text-gray-500">
              {files.slice(0, 8).map((file) => {
                const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
                return <p key={`${file.name}-${file.size}`} className="truncate">{relativePath || file.name}</p>;
              })}
              {files.length > 8 ? <p>还有 {files.length - 8} 个文件…</p> : null}
            </div>
          ) : null}
        </div>
        <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4">
          <button type="button" onClick={onClose} className="h-10 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700">
            取消
          </button>
          <button
            type="button"
            disabled={busy || files.length === 0}
            onClick={() => onImport(files)}
            className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            导入
          </button>
        </div>
      </div>
    </div>
  );
}

function RelationDefinitionEditorModal({
  asset,
  tableAssets,
  databaseSources,
  semanticAssets,
  busy,
  onClose,
  onSave,
}: {
  asset: SemanticAssetDetail;
  tableAssets: TableAsset[];
  databaseSources: KnowledgeDatabaseSource[];
  semanticAssets: SemanticAssetSummary[];
  busy: boolean;
  onClose: () => void;
  onSave: (payload: { name: string; description: string; aliases: string[]; tags: string[]; version: string; relation_definition: AssetRelationDefinition }) => void;
}) {
  const existing = (asset.frontmatter?.relation as Record<string, unknown> | undefined) || {};
  const initialType = (asset.relation_type || "dimension_binding") as AssetRelationDefinition["type"];
  const endpoint = (value: unknown) => (value && typeof value === "object" ? value as { ref?: string; key_fields?: string[] } : {});
  const initialAsset = endpoint(existing.asset);
  const initialDimension = endpoint(existing.dimension);
  const initialLeft = endpoint(existing.left);
  const initialRight = endpoint(existing.right);
  const mapping = existing.field_mapping && typeof existing.field_mapping === "object" ? existing.field_mapping as { left?: string[]; right?: string[] } : {};
  const [type, setType] = useState<AssetRelationDefinition["type"]>(initialType);
  const [name, setName] = useState(asset.name);
  const [description, setDescription] = useState(asset.description || "");
  const [aliases, setAliases] = useState((asset.aliases || []).join(", "));
  const [tags, setTags] = useState((asset.tags || []).join(", "));
  const [version, setVersion] = useState(String(asset.frontmatter?.version || "0.1.0"));
  const [assetRef, setAssetRef] = useState(String(initialAsset.ref || ""));
  const [assetFields, setAssetFields] = useState<string[]>(initialAsset.key_fields || []);
  const [dimensionRef, setDimensionRef] = useState(String(initialDimension.ref || ""));
  const [leftRef, setLeftRef] = useState(String(initialLeft.ref || ""));
  const [leftFields, setLeftFields] = useState<string[]>(mapping.left || initialLeft.key_fields || []);
  const [rightRef, setRightRef] = useState(String(initialRight.ref || ""));
  const [rightFields, setRightFields] = useState<string[]>(mapping.right || initialRight.key_fields || []);
  const [cardinality, setCardinality] = useState<AssetRelationDefinition["cardinality"]>((String(existing.cardinality || "many_to_one")) as AssetRelationDefinition["cardinality"]);
  const sourceOptions = [
    ...tableAssets.map((item) => ({ value: `table_asset:${item.asset_id}`, label: `${item.file_name}${item.sheet_name ? ` · ${item.sheet_name}` : ""}` })),
    ...databaseSources.flatMap((source) => source.selected_tables.length
      ? source.selected_tables.map((table) => ({ value: `${source.id}.${table}`, label: `${source.name} · ${table}` }))
      : [{ value: `database_source:${source.id}`, label: `${source.name} · 未选择表` }]),
  ];
  const { columnsByAsset, loadingByAsset } = useDimensionBindingColumns(
    [{ asset_ref: assetRef }, { asset_ref: leftRef }, { asset_ref: rightRef }],
    tableAssets,
    databaseSources
  );
  const fields = (ref: string) => columnsByAsset[ref] || [];
  const relationDefinition = (): AssetRelationDefinition => type === "dimension_binding"
    ? {
        type,
        asset: { ref: assetRef, display_name: sourceOptions.find((item) => item.value === assetRef)?.label, key_fields: assetFields },
        dimension: { ref: dimensionRef, display_name: semanticAssets.find((item) => item.id === dimensionRef)?.name, output_key: "entity_key" },
        cardinality: "many_to_one",
        grain: assetFields,
        use_statuses: ["auto_matched", "accepted"],
      }
    : {
        type,
        left: { ref: leftRef, display_name: sourceOptions.find((item) => item.value === leftRef)?.label, key_fields: leftFields },
        right: { ref: rightRef, display_name: sourceOptions.find((item) => item.value === rightRef)?.label, key_fields: rightFields },
        field_mapping: { left: leftFields, right: rightFields },
        cardinality,
        join_type: "left",
        grain: { left: leftFields, right: rightFields },
      };

  return <div className="fixed inset-0 z-[145] flex items-center justify-center bg-black/40 px-4 py-6 backdrop-blur-sm">
    <div className="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
      <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5"><div><h3 className="text-lg font-semibold text-gray-950">编辑资产关联</h3><p className="mt-1 text-sm text-gray-500">关系是可复用语义资产；保存后模型只能在已选择它时使用这条路径。</p></div><button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 hover:bg-black/[0.04]"><X className="h-5 w-5" /></button></div>
      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-6 py-5">
        <section className="rounded-3xl border border-black/[0.06] p-4"><h4 className="text-sm font-semibold text-gray-950">基础信息</h4><div className="mt-3 grid gap-3 sm:grid-cols-2"><LabeledInput label="名称" value={name} onChange={setName} placeholder="上险量关联车系" /><LabeledInput label="版本" value={version} onChange={setVersion} placeholder="0.1.0" /><LabeledTextarea label="描述" value={description} onChange={setDescription} placeholder="说明业务键、适用边界与重复计数风险。" /><div className="grid gap-3 sm:grid-cols-2"><LabeledInput label="别名" value={aliases} onChange={setAliases} placeholder="逗号分隔" /><LabeledInput label="标签" value={tags} onChange={setTags} placeholder="逗号分隔" /></div></div></section>
        <section className="rounded-3xl border border-black/[0.06] p-4"><h4 className="text-sm font-semibold text-gray-950">关联方式</h4><div className="mt-3 grid gap-3 sm:grid-cols-2">{(["dimension_binding", "direct_join"] as const).map((value) => <button type="button" key={value} onClick={() => setType(value)} className={`rounded-2xl border p-4 text-left ${type === value ? "border-[#002fa7] bg-[#002fa7]/[0.03]" : "border-black/[0.08] hover:bg-slate-50"}`}><p className="font-semibold text-slate-900">{value === "dimension_binding" ? "关联维度" : "字段关联"}</p><p className="mt-1 text-xs text-slate-500">{value === "dimension_binding" ? "资产字段映射到已发布维度" : "两张资产通过稳定业务键联合"}</p></button>)}</div>
          {type === "dimension_binding" ? <div className="mt-4 grid gap-3 sm:grid-cols-3"><FieldSelect label="数据资产" value={assetRef} options={sourceOptions} onChange={(value) => { setAssetRef(value); setAssetFields([]); }} /><FieldMultiSelect label="来源键字段" values={assetFields} disabled={!assetRef || loadingByAsset[assetRef]} options={fields(assetRef).map((value) => ({ value, label: value }))} onChange={setAssetFields} /><FieldSelect label="已发布维度" value={dimensionRef} options={semanticAssets.filter((item) => item.type === "dimension").map((item) => ({ value: item.id, label: item.name }))} onChange={setDimensionRef} /></div> : <div className="mt-4 space-y-3"><div className="grid gap-3 sm:grid-cols-2"><FieldSelect label="左侧资产" value={leftRef} options={sourceOptions} onChange={(value) => { setLeftRef(value); setLeftFields([]); }} /><FieldMultiSelect label="左侧键字段" values={leftFields} disabled={!leftRef || loadingByAsset[leftRef]} options={fields(leftRef).map((value) => ({ value, label: value }))} onChange={setLeftFields} /><FieldSelect label="右侧资产" value={rightRef} options={sourceOptions} onChange={(value) => { setRightRef(value); setRightFields([]); }} /><FieldMultiSelect label="右侧键字段" values={rightFields} disabled={!rightRef || loadingByAsset[rightRef]} options={fields(rightRef).map((value) => ({ value, label: value }))} onChange={setRightFields} /></div><FieldSelect label="基数" value={cardinality} options={[{ value: "one_to_one", label: "一对一" }, { value: "one_to_many", label: "一对多" }, { value: "many_to_one", label: "多对一" }, { value: "many_to_many", label: "多对多" }]} onChange={(value) => setCardinality(value as AssetRelationDefinition["cardinality"])} /></div>}</section>
      </div>
      <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4"><button type="button" onClick={onClose} className="h-10 rounded-2xl border border-black/[0.08] px-4 text-sm font-semibold text-slate-600">取消</button><button type="button" disabled={busy || !name.trim()} onClick={() => onSave({ name, description, aliases: splitTokenList(aliases), tags: splitTokenList(tags), version: version.trim() || "0.1.0", relation_definition: relationDefinition() })} className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:opacity-45">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}保存关联</button></div>
    </div>
  </div>;
}

function FieldSelect({ label, value, options, onChange, disabled = false }: { label: string; value: string; options: Array<{ value: string; label: string }>; onChange: (value: string) => void; disabled?: boolean }) {
  return <label className="block text-xs font-semibold text-slate-600">{label}<select value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] bg-white px-3 text-sm font-normal text-slate-800 outline-none focus:border-[#002fa7] disabled:bg-slate-50"><option value="">选择{label}</option>{options.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>;
}

function FieldMultiSelect({ label, values, options, onChange, disabled = false }: { label: string; values: string[]; options: Array<{ value: string; label: string }>; onChange: (values: string[]) => void; disabled?: boolean }) {
  const selected = new Set(values);
  const toggle = (value: string) => {
    if (disabled) return;
    onChange(selected.has(value) ? values.filter((item) => item !== value) : [...values, value]);
  };
  return <fieldset className="min-w-0 text-xs font-semibold text-slate-600"><legend>{label}</legend><div className="mt-1.5 flex min-h-10 max-h-28 flex-wrap content-start gap-1.5 overflow-y-auto rounded-xl border border-black/[0.1] bg-white p-2 disabled:bg-slate-50">{options.length ? options.map((item) => <label key={item.value} className={`inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-lg px-2 text-xs font-medium ${selected.has(item.value) ? "bg-[#002fa7]/10 text-[#002fa7]" : "bg-slate-50 text-slate-600"}`}><input type="checkbox" checked={selected.has(item.value)} disabled={disabled} onChange={() => toggle(item.value)} className="accent-[#002fa7]" />{item.label}</label>) : <span className="px-1 py-1 text-slate-400">{disabled ? "先选择资产" : "暂无字段"}</span>}</div><p className="mt-1 text-[11px] font-normal text-slate-400">可选择多个字段组成复合业务键。</p></fieldset>;
}

function SemanticAssetDetailModal({
  asset,
  loading,
  selectedFile,
  editorContent,
  editorOriginal,
  editorLoading,
  editorSaving,
  onClose,
  onSelectFile,
  onChangeContent,
  onSave,
  onEditDimensionDefinition,
  onEditRelationDefinition,
}: {
  asset: SemanticAssetDetail | null;
  loading: boolean;
  selectedFile: SemanticAssetFile | null;
  editorContent: string;
  editorOriginal: string;
  editorLoading: boolean;
  editorSaving: boolean;
  onClose: () => void;
  onSelectFile: (file: SemanticAssetFile) => void;
  onChangeContent: (value: string) => void;
  onSave: () => void;
  onEditDimensionDefinition: (asset: SemanticAssetDetail) => void;
  onEditRelationDefinition: (asset: SemanticAssetDetail) => void;
}) {
  const files = asset?.files || [];
  const dirty = editorContent !== editorOriginal;
  const typeLabel = asset?.type === "dimension" ? "维度" : asset?.type === "grain" ? "颗粒度" : asset?.type === "relation" ? "资产关联" : "度量值";

  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="flex max-h-[88vh] w-full max-w-6xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {asset ? (
                <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-semibold text-[#002fa7]">
                  {typeLabel}
                </span>
              ) : null}
              <h3 className="truncate text-lg font-semibold text-gray-950">{asset?.name || "语义资产"}</h3>
              {dirty ? <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-700">未保存</span> : null}
            </div>
            <p className="mt-1 truncate text-sm text-gray-500">{asset?.path || "正在加载..."}</p>
          </div>
          <div className="flex items-center gap-2">
            {asset?.type === "dimension" ? (
              <button
                type="button"
                onClick={() => onEditDimensionDefinition(asset)}
                className="h-9 rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.04] px-3 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/[0.08]"
              >
                编辑维度设置
              </button>
            ) : null}
            {asset?.type === "relation" ? (
              <button
                type="button"
                onClick={() => onEditRelationDefinition(asset)}
                className="h-9 rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.04] px-3 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/[0.08]"
              >
                编辑关联设置
              </button>
            ) : null}
            <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900">
              <X className="h-5 w-5" />
            </button>
          </div>
        </div>

        {loading ? (
          <div className="flex min-h-[460px] items-center justify-center text-sm text-gray-400">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            正在读取语义资产…
          </div>
        ) : (
          <div className="grid min-h-0 flex-1 grid-cols-[260px_minmax(0,1fr)] overflow-hidden">
            <aside className="min-h-0 border-r border-black/[0.06] bg-slate-50/70 p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-gray-900">
                <FileText className="h-4 w-4 text-[#002fa7]" />
                文件树
              </div>
              <div className="max-h-[620px] space-y-1 overflow-auto">
                {files.map((file) => {
                  const active = selectedFile?.path === file.path;
                  return (
                    <button
                      key={file.path}
                      type="button"
                      onClick={() => onSelectFile(file)}
                      className={`flex w-full items-center justify-between gap-2 rounded-xl px-3 py-2 text-left text-xs transition ${
                        active ? "bg-[#002fa7] text-white" : "bg-white text-gray-600 hover:bg-[#002fa7]/[0.06] hover:text-[#002fa7]"
                      }`}
                      title={file.relative_path}
                    >
                      <span className="min-w-0 truncate font-mono">{file.relative_path}</span>
                      {file.main ? (
                        <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] ${active ? "bg-white/15 text-white" : "bg-[#002fa7]/10 text-[#002fa7]"}`}>
                          main
                        </span>
                      ) : null}
                    </button>
                  );
                })}
                {files.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-black/[0.08] px-3 py-8 text-center text-xs text-gray-400">
                    没有文件
                  </div>
                ) : null}
              </div>
            </aside>
            <section className="flex min-h-0 min-w-0 flex-col">
              <div className="flex items-center justify-between gap-3 border-b border-black/[0.06] px-4 py-3">
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs font-semibold text-gray-800">{selectedFile?.path || "未选择文件"}</p>
                  <p className="mt-0.5 text-xs text-gray-400">
                    {selectedFile?.editable ? "Markdown / 文本可编辑" : "当前文件不可编辑"}
                  </p>
                </div>
                <button
                  type="button"
                  disabled={!selectedFile?.editable || editorLoading || editorSaving || !dirty}
                  onClick={onSave}
                  className="inline-flex h-9 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {editorSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                  保存
                </button>
              </div>
              <div className="min-h-0 flex-1 p-4">
                {editorLoading ? (
                  <div className="flex h-full min-h-[420px] items-center justify-center rounded-2xl border border-dashed border-black/[0.08] text-sm text-gray-400">
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    正在读取文件…
                  </div>
                ) : (
                  <textarea
                    value={editorContent}
                    onChange={(event) => onChangeContent(event.target.value)}
                    disabled={!selectedFile?.editable}
                    spellCheck={false}
                    className="h-full min-h-[520px] w-full resize-none rounded-2xl border border-black/[0.08] bg-gray-950 p-4 font-mono text-xs leading-5 text-gray-100 outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08] disabled:opacity-60"
                    placeholder="选择一个 Markdown 文件"
                  />
                )}
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

function taskTypeLabel(type: string): string {
  if (type === "semantic_dimension_build") return "语义维度";
  if (type === "knowledge_import") return "知识导入";
  return "后台任务";
}

function taskStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "处理中",
    waiting_for_publish_confirmation: "等待发布确认",
    waiting_for_baseline_change_confirmation: "待处理规范基准变更",
    published: "已发布",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status] || status || "未知状态";
}

function TaskCenterSection({
  tasks,
  loading,
  onRefresh,
  onOpenTask,
}: {
  tasks: TaskCenterItem[];
  loading: boolean;
  onRefresh: () => void;
  onOpenTask: (task: TaskCenterItem) => void;
}) {
  return (
    <section className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-black/[0.06] pb-5">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.06] text-[#002fa7]">
            <ListTodo className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-gray-950">任务中心</h2>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-gray-500">
              统一查看后台任务的状态和进度。知识导入、语义维度构建各自保持独立执行，这里只做集中查看。
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="inline-flex h-10 items-center gap-2 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          刷新任务
        </button>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-3">
        <MetricCard icon={ListTodo} title="全部任务" value={tasks.length} tone="blue" compact />
        <MetricCard icon={Loader2} title="进行中" value={tasks.filter((item) => ["queued", "running"].includes(String(item.job.status || ""))).length} tone="orange" compact />
        <MetricCard icon={CheckCircle2} title="待处理" value={tasks.filter((item) => ["waiting_for_publish_confirmation", "waiting_for_baseline_change_confirmation"].includes(String(item.job.status || ""))).length} tone="orange" compact />
      </div>

      <div className="mt-5 space-y-2">
        {loading ? (
          <div className="flex min-h-56 items-center justify-center rounded-2xl border border-dashed border-black/[0.08] text-sm text-gray-400">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            正在读取后台任务…
          </div>
        ) : tasks.length === 0 ? (
          <EmptyWorkbenchState title="暂无后台任务" description="发起知识导入、实体导入或语义维度构建后，任务会出现在这里。" />
        ) : (
          tasks.map((task) => {
            const status = String(task.job.status || "");
            const progress = Number(task.job.progress || 0);
            const isWaitingPublish = status === "waiting_for_publish_confirmation";
            const isBaselineChange = status === "waiting_for_baseline_change_confirmation";
            return (
              <button
                key={`${task.task_type}:${task.job.id}`}
                type="button"
                onClick={() => onOpenTask(task)}
                className="w-full rounded-2xl border border-black/[0.06] bg-white p-4 text-left transition hover:border-[#002fa7]/35 hover:shadow-sm focus:outline-none focus:ring-4 focus:ring-[#002fa7]/[0.08]"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${task.task_type === "semantic_dimension_build" ? "bg-[#002fa7]/[0.08] text-[#002fa7]" : "bg-gray-100 text-gray-600"}`}>
                        {taskTypeLabel(task.task_type)}
                      </span>
                      <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                        status === "failed" ? "bg-red-50 text-red-700" : isWaitingPublish || isBaselineChange ? "bg-amber-50 text-amber-700" : status === "succeeded" ? "bg-emerald-50 text-emerald-700" : "bg-blue-50 text-blue-700"
                      }`}>
                        {taskStatusLabel(status)}
                      </span>
                    </div>
                    <h3 className="mt-2 truncate text-sm font-semibold text-gray-900">{task.title}</h3>
                    <p className="mt-1 font-mono text-[11px] text-gray-400">{String(task.job.id || "-")}</p>
                  </div>
                  <div className="shrink-0 text-right">
                    <div className="text-lg font-semibold text-gray-800">{progress}%</div>
                    <div className="mt-1 text-[11px] text-gray-400">{formatDateTime(task.created_at)}</div>
                  </div>
                </div>
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-black/[0.06]">
                  <div className={`h-full rounded-full ${status === "failed" ? "bg-red-500" : isWaitingPublish || isBaselineChange ? "bg-amber-500" : "bg-[#002fa7]"}`} style={{ width: `${Math.max(0, Math.min(100, progress))}%` }} />
                </div>
                {isBaselineChange ? (
                  <p className="mt-3 text-xs leading-5 text-amber-700">规范基准发生变化，staging 尚未影响活跃维度。请到该维度的匹配管理处理。</p>
                ) : isWaitingPublish ? (
                  <p className="mt-3 text-xs leading-5 text-amber-700">构建产物已落在 staging，尚未影响活跃维度。请回到原对话明确要求发布。</p>
                ) : task.job.error_message ? (
                  <p className="mt-3 line-clamp-2 text-xs leading-5 text-red-600">{String(task.job.error_message)}</p>
                ) : null}
              </button>
            );
          })
        )}
      </div>
    </section>
  );
}

function TaskDetailModal({
  detail,
  loading,
  onClose,
  onOpenDimensionMatching,
}: {
  detail: TaskDetailState;
  loading: boolean;
  onClose: () => void;
  onOpenDimensionMatching: (dimensionId: string) => void;
}) {
  const status = String(detail.job.status || detail.task.job.status || "");
  const isSemanticBuild = detail.task.task_type === "semantic_dimension_build";
  const resultSummary = detail.job.result_summary;
  const stagingPath = String(detail.job.staging_path || "");
  const errorMessage = String(detail.job.error_message || "");
  const createdAt = typeof detail.job.created_at === "string" ? detail.job.created_at : detail.task.created_at;

  return (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
      <div className="flex max-h-[88vh] w-full max-w-4xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full bg-[#002fa7]/[0.08] px-2 py-0.5 text-[11px] font-semibold text-[#002fa7]">{taskTypeLabel(detail.task.task_type)}</span>
              <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${status === "failed" ? "bg-red-50 text-red-700" : status === "waiting_for_publish_confirmation" || status === "waiting_for_baseline_change_confirmation" ? "bg-amber-50 text-amber-700" : status === "succeeded" || status === "published" ? "bg-emerald-50 text-emerald-700" : "bg-blue-50 text-blue-700"}`}>
                {taskStatusLabel(status)}
              </span>
            </div>
            <h3 className="mt-2 truncate text-lg font-semibold text-gray-950">{detail.task.title}</h3>
            <p className="mt-1 font-mono text-xs text-gray-400">{String(detail.job.id || detail.task.job.id || "-")}</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-gray-900" aria-label="关闭">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          {loading ? (
            <div className="flex min-h-64 items-center justify-center text-sm text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在读取任务明细…</div>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-3">
                <MetricCard icon={ListTodo} title="状态" value={taskStatusLabel(status)} tone={status === "failed" ? "orange" : status === "succeeded" || status === "published" ? "green" : "blue"} compact />
                <MetricCard icon={RefreshCw} title="进度" value={`${Number(detail.job.progress || 0)}%`} tone={status === "waiting_for_publish_confirmation" || status === "waiting_for_baseline_change_confirmation" ? "orange" : "blue"} compact />
                <MetricCard icon={CheckCircle2} title="创建时间" value={formatDateTime(createdAt)} tone="blue" compact />
              </div>

              {isSemanticBuild && status === "waiting_for_publish_confirmation" ? (
                <div className="mt-5 rounded-2xl border border-amber-500/15 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-800">
                  构建已完成并通过校验，但尚未发布。staging 结果不会参与正式分析；请回到原对话明确要求 Agent 发布。
                </div>
              ) : null}
              {isSemanticBuild && status === "waiting_for_baseline_change_confirmation" ? (
                <div className="mt-5 rounded-2xl border border-amber-500/15 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-800">
                  <p>构建发现规范基准变化，staging 已保留且尚未影响活跃维度。请在匹配管理的“全局总览”中处理停用、移除或取消本次变更。</p>
                  <button type="button" onClick={() => onOpenDimensionMatching(String(detail.job.dimension_id || detail.task.job.dimension_id || ""))} className="mt-3 inline-flex h-8 items-center rounded-lg bg-[#002fa7] px-3 text-xs font-semibold text-white">打开匹配管理</button>
                </div>
              ) : null}
              {errorMessage ? <div className="mt-5 rounded-2xl border border-red-500/15 bg-red-50 px-4 py-3 text-sm leading-6 text-red-700">{errorMessage}</div> : null}

              {stagingPath ? (
                <section className="mt-5 rounded-2xl border border-black/[0.06] p-4">
                  <h4 className="text-sm font-semibold text-gray-950">Staging 产物</h4>
                  <p className="mt-2 break-all font-mono text-xs leading-5 text-gray-500">{stagingPath}</p>
                </section>
              ) : null}

              {resultSummary && typeof resultSummary === "object" ? (
                <section className="mt-5 rounded-2xl border border-black/[0.06] p-4">
                  <h4 className="text-sm font-semibold text-gray-950">构建摘要</h4>
                  <pre className="mt-3 max-h-72 overflow-auto rounded-xl bg-gray-950 p-4 text-xs leading-5 text-gray-100">{JSON.stringify(resultSummary, null, 2)}</pre>
                </section>
              ) : null}

              <section className="mt-5 rounded-2xl border border-black/[0.06] p-4">
                <div className="flex items-center justify-between gap-2"><h4 className="text-sm font-semibold text-gray-950">执行事件</h4><span className="text-xs text-gray-400">{detail.events.length} 条</span></div>
                {detail.events.length ? (
                  <ol className="mt-3 space-y-3">
                    {detail.events.map((event) => (
                      <li key={event.id} className="border-l-2 border-[#002fa7]/20 pl-3">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs"><span className={event.level === "error" ? "font-semibold text-red-600" : "font-semibold text-[#002fa7]"}>{event.level}</span><span className="text-gray-400">{formatDateTime(event.created_at)}</span></div>
                        <p className="mt-1 text-sm leading-5 text-gray-700">{event.message}</p>
                      </li>
                    ))}
                  </ol>
                ) : <p className="mt-3 text-sm text-gray-400">没有可显示的事件记录。</p>}
              </section>
            </>
          )}
        </div>
      </div>
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
  onRemove,
}: {
  source: KnowledgeDatabaseSource;
  onManage: (source: KnowledgeDatabaseSource) => void;
  onTrainTable: (source: KnowledgeDatabaseSource, table: string) => void;
  onRemove: (source: KnowledgeDatabaseSource) => void;
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
        <div className="flex shrink-0 items-center gap-2">
          {!source.builtin ? (
            <button
              type="button"
              onClick={() => onRemove(source)}
              className="inline-flex h-9 w-9 items-center justify-center rounded-xl border border-black/[0.08] bg-white text-gray-500 shadow-sm transition hover:border-red-200 hover:bg-red-50 hover:text-red-600"
              title="移除数据库资产"
              aria-label={`移除数据库资产 ${source.name}`}
            >
              <Trash2 className="h-4 w-4" />
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => onManage(source)}
            className="rounded-2xl border border-black/[0.08] bg-white px-3.5 py-2 text-xs font-semibold text-gray-700 shadow-sm transition hover:bg-gray-50"
          >
            管理
          </button>
        </div>
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
            <p className="mt-1 text-sm text-gray-500">保存连接信息和可用表，后续分析模型会从这里选择数据。</p>
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
                <p className="mt-1 text-xs text-gray-400">只保存你希望分析模型和问数使用的表。</p>
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

function RemoveTableAssetDialog({
  asset,
  removing,
  onClose,
  onConfirm,
}: {
  asset: TableAsset;
  removing: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 backdrop-blur-sm">
      <section className="w-full max-w-md rounded-[18px] bg-white p-6 shadow-2xl" role="dialog" aria-modal="true" aria-label="移除数据资产">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-red-50 text-red-600"><Trash2 className="h-5 w-5" /></div>
        <h2 className="mt-4 text-lg font-semibold text-gray-950">从智能问数移除？</h2>
        <p className="mt-2 break-all text-sm leading-6 text-gray-600">{asset.file_name}{asset.sheet_name ? ` · ${asset.sheet_name}` : ""}</p>
        <p className="mt-3 text-sm leading-6 text-gray-500">将移除该文件的全部工作表资产及其 Profile。知识库原始文件不会被删除；引用它的模型或维度需要后续更新绑定。</p>
        <div className="mt-6 flex justify-end gap-3">
          <button type="button" onClick={onClose} disabled={removing} className="h-10 rounded-xl border border-black/[0.08] px-4 text-sm font-semibold text-gray-700 transition hover:bg-gray-50 disabled:opacity-50">取消</button>
          <button type="button" onClick={onConfirm} disabled={removing} className="inline-flex h-10 items-center gap-2 rounded-xl bg-red-600 px-4 text-sm font-semibold text-white transition hover:bg-red-700 disabled:opacity-50">
            {removing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
            移除
          </button>
        </div>
      </section>
    </div>
  );
}

function RemoveDatabaseSourceDialog({
  source,
  removing,
  onClose,
  onConfirm,
}: {
  source: KnowledgeDatabaseSource;
  removing: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/35 px-4 backdrop-blur-sm">
      <section className="w-full max-w-md rounded-[18px] bg-white p-6 shadow-2xl" role="dialog" aria-modal="true" aria-label="移除数据库资产">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-red-50 text-red-600"><Trash2 className="h-5 w-5" /></div>
        <h2 className="mt-4 text-lg font-semibold text-gray-950">移除数据库资产？</h2>
        <p className="mt-2 break-all text-sm leading-6 text-gray-600">{source.name}</p>
        <p className="mt-3 text-sm leading-6 text-gray-500">将删除智能问数保存的连接配置及已选表信息，不会删除外部数据库中的表或数据；引用此数据源的模型、维度和 SQL 守卫需要后续更新。</p>
        <div className="mt-6 flex justify-end gap-3">
          <button type="button" onClick={onClose} disabled={removing} className="h-10 rounded-xl border border-black/[0.08] px-4 text-sm font-semibold text-gray-700 transition hover:bg-gray-50 disabled:opacity-50">取消</button>
          <button type="button" onClick={onConfirm} disabled={removing} className="inline-flex h-10 items-center gap-2 rounded-xl bg-red-600 px-4 text-sm font-semibold text-white transition hover:bg-red-700 disabled:opacity-50">
            {removing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
            移除
          </button>
        </div>
      </section>
    </div>
  );
}

function ConcatDatasetModal({
  assets,
  busy,
  onClose,
  onCreate,
}: {
  assets: TableAsset[];
  busy: boolean;
  onClose: () => void;
  onCreate: (payload: { name: string; description: string; tags: string[]; sourceAssetIds: string[]; schemaMode: "strict" | "baseline_fill_missing" | "union_fill_missing"; preferredIntents: string[]; directSourceAllowed: boolean }) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<ConcatDatasetPreview | null>(null);
  const [schemaMode, setSchemaMode] = useState<"baseline_fill_missing" | "union_fill_missing">("union_fill_missing");
  const [previewing, setPreviewing] = useState(false);
  const [error, setError] = useState("");
  const toggle = (assetId: string) => setSelected((current) => current.includes(assetId) ? current.filter((item) => item !== assetId) : [...current, assetId]);
  const inspect = async () => {
    setPreviewing(true);
    setError("");
    try {
      setPreview(await previewConcatDataset(selected));
    } catch (nextError) {
      setPreview(null);
      setError(errorMessage(nextError));
    } finally {
      setPreviewing(false);
    }
  };
  const resetPreview = () => {
    setPreview(null);
    setError("");
  };
  return <div className="fixed inset-0 z-[150] flex items-center justify-center bg-black/40 px-4 py-6 backdrop-blur-sm">
    <div className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
      <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5"><div><h3 className="text-lg font-semibold text-gray-950">合并表格为逻辑数据集</h3><p className="mt-1 text-sm text-gray-500">适用于月度、周度等同口径表。先检查字段差异；确认后缺失字段补空，额外字段保留。</p></div><button type="button" onClick={onClose} disabled={busy || previewing} className="rounded-full p-2 text-gray-400 hover:bg-black/[0.04]"><X className="h-5 w-5" /></button></div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
        <div className="grid gap-3 sm:grid-cols-2"><label className="block text-sm font-semibold text-slate-700">逻辑数据集名称<input value={name} onChange={(event) => { setName(event.target.value); resetPreview(); }} placeholder="例如：2023年乘用车上险量" className="mt-2 h-11 w-full rounded-2xl border border-black/[0.1] px-3 text-sm font-normal outline-none focus:border-[#002fa7]" /></label><label className="block text-sm font-semibold text-slate-700">标签<input value={tagsText} onChange={(event) => setTagsText(event.target.value)} placeholder="销量, 上险量, 月度" className="mt-2 h-11 w-full rounded-2xl border border-black/[0.1] px-3 text-sm font-normal outline-none focus:border-[#002fa7]" /></label></div>
        <label className="block text-sm font-semibold text-slate-700">业务定义与使用说明<textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="说明数据集包含什么；什么问题应优先使用它；何时可直接查询原始来源；哪些场景不适用。" className="mt-2 min-h-24 w-full resize-y rounded-2xl border border-black/[0.1] px-3 py-2 text-sm font-normal outline-none focus:border-[#002fa7]" /></label>
        <div className="rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.03] px-4 py-3 text-xs leading-5 text-[#00246f]">创建时仅保存来源、字段策略和行级溯源规则，不会读取全量数据或生成 Parquet。后续分析命中它时才按需联合这些来源；以后只需绑定一次车系等维度。</div>
        <section className="overflow-hidden rounded-2xl border border-black/[0.08]"><div className="flex items-center justify-between bg-slate-50 px-4 py-3"><p className="text-sm font-semibold text-slate-800">选择来源表</p><span className="text-xs text-slate-500">已选 {selected.length} 张</span></div><div className="max-h-72 divide-y divide-black/[0.05] overflow-auto">{assets.map((asset) => <label key={asset.asset_id} className="flex cursor-pointer items-center gap-3 px-4 py-3 hover:bg-slate-50"><input type="checkbox" checked={selected.includes(asset.asset_id)} onChange={() => { toggle(asset.asset_id); resetPreview(); }} className="h-4 w-4 accent-[#002fa7]" /><div className="min-w-0 flex-1"><p className="truncate text-sm font-semibold text-slate-800">{asset.file_name}</p><p className="mt-1 text-xs text-slate-400">{sourceTypeLabel(asset)} · {asset.columns_count ?? "-"} 列 · {typeof asset.rows === "number" ? `${asset.rows} 行` : "行数待 Profile"}</p></div></label>)}{assets.length === 0 ? <p className="px-4 py-10 text-center text-sm text-slate-400">没有可用于合并的原始表格资产。</p> : null}</div></section>
        {error ? <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div> : null}
        {preview ? <ConcatDatasetSchemaPreview preview={preview} schemaMode={schemaMode} onSchemaModeChange={setSchemaMode} /> : null}
      </div>
      <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4"><button type="button" onClick={onClose} disabled={busy || previewing} className="h-10 rounded-2xl border border-black/[0.08] px-4 text-sm font-semibold text-slate-600">取消</button>{!preview ? <button type="button" disabled={previewing || !name.trim() || selected.length < 2} onClick={() => void inspect()} className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:opacity-45">{previewing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}检查字段</button> : <button type="button" disabled={busy} onClick={() => onCreate({ name, description, tags: tagsText.split(/[,，]/).map((item) => item.trim()).filter(Boolean), sourceAssetIds: selected, schemaMode: preview.has_schema_drift ? schemaMode : "strict", preferredIntents: [], directSourceAllowed: true })} className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:opacity-45">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Layers3 className="h-4 w-4" />}确认创建</button>}</div>
    </div>
  </div>;
}

function ConcatDatasetSchemaPreview({ preview, schemaMode, onSchemaModeChange }: { preview: ConcatDatasetPreview; schemaMode: "baseline_fill_missing" | "union_fill_missing"; onSchemaModeChange: (value: "baseline_fill_missing" | "union_fill_missing") => void }) {
  const changed = preview.sources.filter((source) => source.missing_from_baseline.length || source.extra_vs_baseline.length);
  if (!preview.has_schema_drift) return <div className="rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">字段一致，共 {preview.canonical_columns.length} 个字段；可以直接合并。</div>;
  return <section className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-4"><p className="text-sm font-semibold text-amber-900">发现字段差异</p><p className="mt-1 text-xs leading-5 text-amber-800">请选择多出字段的处理方式。无论哪种方式，缺少的保留字段都会在对应来源行置空；原始文件不会被修改。</p><div className="mt-3 grid gap-2 sm:grid-cols-2"><button type="button" onClick={() => onSchemaModeChange("union_fill_missing")} className={`rounded-xl border p-3 text-left ${schemaMode === "union_fill_missing" ? "border-[#002fa7] bg-white text-[#00246f]" : "border-amber-200 bg-white/60 text-slate-600"}`}><span className="block text-sm font-semibold">保留多余字段并补空</span><span className="mt-1 block text-xs leading-5">字段并集进入逻辑数据集；其他表该字段为空。</span></button><button type="button" onClick={() => onSchemaModeChange("baseline_fill_missing")} className={`rounded-xl border p-3 text-left ${schemaMode === "baseline_fill_missing" ? "border-[#002fa7] bg-white text-[#00246f]" : "border-amber-200 bg-white/60 text-slate-600"}`}><span className="block text-sm font-semibold">丢弃多余字段</span><span className="mt-1 block text-xs leading-5">只保留基准表字段；缺少的基准字段补空。</span></button></div><div className="mt-3 space-y-2">{changed.map((source) => <div key={source.asset_id} className="rounded-xl bg-white/80 px-3 py-2 text-xs text-slate-700"><span className="font-semibold">{source.file_name}</span>{source.missing_from_baseline.length ? <p className="mt-1">缺少：{source.missing_from_baseline.join("、")}</p> : null}{source.extra_vs_baseline.length ? <p className="mt-1">多出：{source.extra_vs_baseline.join("、")}</p> : null}</div>)}</div></section>;
}

function AppendConcatDatasetSourcesModal({
  dataset,
  assets,
  busy,
  onClose,
  onAppend,
}: {
  dataset: TableAsset;
  assets: TableAsset[];
  busy: boolean;
  onClose: () => void;
  onAppend: (payload: { asset: TableAsset; sourceAssetIds: string[]; schemaMode: "strict" | "baseline_fill_missing" | "union_fill_missing" }) => void;
}) {
  const existingIds = dataset.logical_dataset?.source_asset_ids ?? [];
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<ConcatDatasetPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [schemaMode, setSchemaMode] = useState<"baseline_fill_missing" | "union_fill_missing">("union_fill_missing");
  const [error, setError] = useState("");
  const toggle = (assetId: string) => {
    setSelected((current) => current.includes(assetId) ? current.filter((item) => item !== assetId) : [...current, assetId]);
    setPreview(null);
    setError("");
  };
  const inspect = async () => {
    setPreviewing(true);
    setError("");
    try {
      setPreview(await previewConcatDataset([...existingIds, ...selected]));
    } catch (nextError) {
      setPreview(null);
      setError(errorMessage(nextError));
    } finally {
      setPreviewing(false);
    }
  };
  const selectableAssets = assets.filter((asset) => !existingIds.includes(asset.asset_id));
  return <div className="fixed inset-0 z-[150] flex items-center justify-center bg-black/40 px-4 py-6 backdrop-blur-sm">
    <div className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
      <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5"><div><h3 className="text-lg font-semibold text-gray-950">追加来源表</h3><p className="mt-1 text-sm text-gray-500">追加到“{dataset.file_name}”，会保留现有来源与行级溯源。</p></div><button type="button" onClick={onClose} disabled={busy || previewing} className="rounded-full p-2 text-gray-400 hover:bg-black/[0.04]"><X className="h-5 w-5" /></button></div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
        <div className="rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.03] px-4 py-3 text-xs leading-5 text-[#00246f]">当前已登记 {existingIds.length} 张来源表。先检查“现有 + 新增”全部字段，再确认追加；不会修改任何原始文件。</div>
        <section className="overflow-hidden rounded-2xl border border-black/[0.08]"><div className="flex items-center justify-between bg-slate-50 px-4 py-3"><p className="text-sm font-semibold text-slate-800">选择新来源表</p><span className="text-xs text-slate-500">已选 {selected.length} 张</span></div><div className="max-h-72 divide-y divide-black/[0.05] overflow-auto">{selectableAssets.map((asset) => <label key={asset.asset_id} className="flex cursor-pointer items-center gap-3 px-4 py-3 hover:bg-slate-50"><input type="checkbox" checked={selected.includes(asset.asset_id)} onChange={() => toggle(asset.asset_id)} className="h-4 w-4 accent-[#002fa7]" /><div className="min-w-0 flex-1"><p className="truncate text-sm font-semibold text-slate-800">{asset.file_name}</p><p className="mt-1 text-xs text-slate-400">{sourceTypeLabel(asset)} · {asset.columns_count ?? "-"} 列</p></div></label>)}{selectableAssets.length === 0 ? <p className="px-4 py-10 text-center text-sm text-slate-400">没有可追加的原始表格资产。</p> : null}</div></section>
        {error ? <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div> : null}
        {preview ? <ConcatDatasetSchemaPreview preview={preview} schemaMode={schemaMode} onSchemaModeChange={setSchemaMode} /> : null}
      </div>
      <div className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4"><button type="button" onClick={onClose} disabled={busy || previewing} className="h-10 rounded-2xl border border-black/[0.08] px-4 text-sm font-semibold text-slate-600">取消</button>{!preview ? <button type="button" disabled={previewing || selected.length === 0} onClick={() => void inspect()} className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:opacity-45">{previewing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}检查字段</button> : <button type="button" disabled={busy} onClick={() => onAppend({ asset: dataset, sourceAssetIds: selected, schemaMode: preview.has_schema_drift ? schemaMode : "strict" })} className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:opacity-45">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Layers3 className="h-4 w-4" />}确认追加来源</button>}</div>
    </div>
  </div>;
}

function TableAssetCard({
  asset,
  profileLoadingId,
  profilingAssetId,
  onOpenProfile,
  onOpenDefinition,
  onGenerateProfile,
  onRefreshConcat,
  onAppendConcat,
  refreshingConcat,
  onRemove,
}: {
  asset: TableAsset;
  profileLoadingId: string | null;
  profilingAssetId: string | null;
  onOpenProfile: (asset: TableAsset) => void;
  onOpenDefinition: (asset: TableAsset) => void;
  onGenerateProfile: (asset: TableAsset) => void;
  onRefreshConcat: (asset: TableAsset) => void;
  onAppendConcat: (asset: TableAsset) => void;
  refreshingConcat: boolean;
  onRemove: (asset: TableAsset) => void;
}) {
  const isVirtualLogicalDataset = asset.source_type === "logical_concat";
  const [moreActionsOpen, setMoreActionsOpen] = useState(false);
  const moreActionsRef = useRef<HTMLDetailsElement>(null);
  const primaryActionClass = "inline-flex h-9 items-center gap-1.5 rounded-xl bg-[#002fa7] px-3 text-xs font-semibold text-white transition hover:bg-[#00246f] disabled:cursor-wait disabled:opacity-60";
  const secondaryActionClass = "inline-flex h-9 items-center gap-1.5 rounded-xl border border-[#002fa7]/20 bg-white px-3 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/[0.04] disabled:cursor-wait disabled:opacity-60";

  useEffect(() => {
    if (!moreActionsOpen) return;

    const closeOnOutsideInteraction = (event: PointerEvent) => {
      if (!moreActionsRef.current?.contains(event.target as Node)) {
        setMoreActionsOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMoreActionsOpen(false);
    };

    document.addEventListener("pointerdown", closeOnOutsideInteraction);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideInteraction);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [moreActionsOpen]);

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
                  : asset.profile_status === "partial"
                    ? "bg-amber-50 text-amber-700"
                    : "bg-orange-50 text-orange-700"
              }`}
            >
              {asset.profile_status === "ready"
                ? "Profile 可用"
                : asset.profile_status === "partial"
                  ? "Profile 待补充"
                  : "Profile 待生成"}
            </span>
          </div>
          <p className="mt-2 break-all text-xs text-gray-400">
            {isVirtualLogicalDataset
              ? `系统托管的 dataset.json 定义，不复制来源数据${asset.logical_dataset?.source_asset_ids?.length ? ` · ${asset.logical_dataset.source_asset_ids.length} 个来源` : ""}`
              : asset.virtual_path}
          </p>
          <div className="mt-3 flex flex-wrap gap-2 text-xs text-gray-500">
            <span>{formatBytes(asset.size_bytes)}</span>
            {typeof asset.rows === "number" ? <span>{asset.rows} 行</span> : null}
            {typeof asset.columns_count === "number" ? <span>{asset.columns_count} 列</span> : null}
            {asset.columns?.length ? <span>字段：{asset.columns.slice(0, 5).join("、")}</span> : null}
            {asset.logical_dataset?.source_asset_ids?.length ? <span>来源：{asset.logical_dataset.source_asset_ids.length} 张表</span> : null}
          </div>
        </div>
        <div className="flex shrink-0 items-start gap-2">
          {isVirtualLogicalDataset ? (
            <button
              type="button"
              onClick={() => onOpenDefinition(asset)}
              className={primaryActionClass}
            >
              <FileText className="h-3.5 w-3.5" />
              管理
            </button>
          ) : null}
          {!isVirtualLogicalDataset && asset.profile_status === "ready" ? (
            <button
              type="button"
              onClick={() => onOpenProfile(asset)}
              disabled={profileLoadingId === asset.asset_id}
              className={primaryActionClass}
            >
              <FileText className="h-3.5 w-3.5" />
              {profileLoadingId === asset.asset_id ? "打开中" : "查看 Profile"}
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => onGenerateProfile(asset)}
            disabled={profilingAssetId === asset.asset_id}
            className={secondaryActionClass}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${profilingAssetId === asset.asset_id ? "animate-spin" : ""}`} />
            {profilingAssetId === asset.asset_id ? "更新中" : isVirtualLogicalDataset ? "更新 Profile" : asset.profile_status === "ready" ? "重新生成" : "生成"}
          </button>
          <details ref={moreActionsRef} open={moreActionsOpen} className="relative">
            <summary onClick={(event) => { event.preventDefault(); setMoreActionsOpen((open) => !open); }} className="flex h-9 w-9 cursor-pointer list-none items-center justify-center rounded-xl border border-black/[0.08] text-gray-500 transition hover:bg-slate-50 [&::-webkit-details-marker]:hidden" title="更多操作" aria-label={`更多操作：${asset.file_name}`} aria-expanded={moreActionsOpen}>
              <MoreHorizontal className="h-4 w-4" />
            </summary>
            <div className="absolute right-0 z-30 mt-2 w-40 overflow-hidden rounded-xl border border-black/[0.08] bg-white p-1 shadow-xl">
              {isVirtualLogicalDataset && asset.profile_status !== "missing" ? <button type="button" onClick={() => { setMoreActionsOpen(false); onOpenProfile(asset); }} className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs font-medium text-gray-700 hover:bg-slate-50"><FileText className="h-3.5 w-3.5" />查看 Profile</button> : null}
              {asset.source_type === "derived_concat" || asset.source_type === "logical_concat" ? <button type="button" onClick={() => { setMoreActionsOpen(false); onAppendConcat(asset); }} className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs font-medium text-gray-700 hover:bg-slate-50"><Layers3 className="h-3.5 w-3.5" />追加来源</button> : null}
              {asset.source_type === "derived_concat" || asset.source_type === "logical_concat" ? <button type="button" onClick={() => { setMoreActionsOpen(false); onRefreshConcat(asset); }} disabled={refreshingConcat} className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs font-medium text-gray-700 hover:bg-slate-50 disabled:opacity-50"><RefreshCw className={`h-3.5 w-3.5 ${refreshingConcat ? "animate-spin" : ""}`} />{asset.source_type === "logical_concat" ? "刷新定义" : "刷新合并"}</button> : null}
              <button type="button" onClick={() => { setMoreActionsOpen(false); onRemove(asset); }} className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs font-medium text-red-600 hover:bg-red-50"><Trash2 className="h-3.5 w-3.5" />从智能问数移除</button>
            </div>
          </details>
        </div>
      </div>
    </article>
  );
}

function LogicalDatasetDefinitionModal({ asset, onClose, onEdit }: { asset: TableAsset; onClose: () => void; onEdit: () => void }) {
  const definition = asset.logical_dataset!;
  const fields = definition.schema?.fields ?? definition.canonical_columns ?? [];
  const sources = definition.sources ?? [];
  const coverage = definition.coverage ?? [];
  const profile = definition.profile;
  return (
    <div className="fixed inset-0 z-[150] flex items-center justify-center bg-black/40 px-4 py-6 backdrop-blur-sm">
      <section className="flex max-h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-[20px] bg-white shadow-2xl" role="dialog" aria-modal="true" aria-label="逻辑数据集定义">
        <header className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="min-w-0">
            <p className="text-sm font-semibold text-[#002fa7]">虚拟逻辑数据集定义</p>
            <h2 className="mt-1 truncate text-xl font-semibold text-gray-950">{asset.file_name}</h2>
            <p className="mt-1 text-sm text-gray-500">系统托管的 dataset.json；创建时不复制来源数据。</p>
          </div>
          <div className="flex items-center gap-2"><button type="button" onClick={onEdit} className="h-9 rounded-xl border border-[#002fa7]/20 px-3 text-xs font-semibold text-[#002fa7] hover:bg-[#002fa7]/[0.04]">编辑定义</button><button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 hover:bg-black/[0.04]" aria-label="关闭"><X className="h-5 w-5" /></button></div>
        </header>
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-6 py-5">
          <div className="grid gap-3 sm:grid-cols-3">
            <DefinitionStat label="来源表" value={`${definition.statistics?.source_count ?? sources.length} 张`} />
            <DefinitionStat label="行数估计" value={typeof definition.statistics?.rows_estimate === "number" ? definition.statistics.rows_estimate.toLocaleString() : "待来源 Profile"} />
            <DefinitionStat label="字段策略" value={definition.schema_mode === "strict" ? "字段严格一致" : definition.schema_mode === "union_fill_missing" ? "保留全部字段" : "仅保留基准字段"} />
          </div>
          <section className="rounded-lg border border-black/[0.08] bg-slate-50 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-950">Profile 与新鲜度</h3><p className="mt-1 text-xs text-gray-500">不物化合并表；统计由各来源 Profile 汇总。</p></div><span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${profile?.status === "ready" ? "bg-emerald-50 text-emerald-700" : profile?.status === "partial" ? "bg-amber-50 text-amber-700" : "bg-slate-200 text-slate-600"}`}>{profile?.status === "ready" ? "全部来源已就绪" : profile?.status === "partial" ? "部分来源待更新" : "尚未生成"}</span></div>
            <div className="mt-3 grid gap-3 sm:grid-cols-3"><DefinitionStat label="已生成 Profile" value={`${profile?.profiled_source_count ?? 0}/${profile?.source_count ?? sources.length} 个来源`} /><DefinitionStat label="新鲜来源" value={`${profile?.fresh_source_count ?? 0}/${profile?.source_count ?? sources.length} 个来源`} /><DefinitionStat label="覆盖字段" value={coverage.length ? `${coverage.length} 个来源含时间覆盖` : "待来源 Profile"} /></div>
            {profile?.profile_refreshed_at || definition.profile_refreshed_at ? <p className="mt-3 text-xs text-gray-500">摘要更新时间：{formatDateTime(profile?.profile_refreshed_at || definition.profile_refreshed_at || "")}</p> : null}
          </section>
          <section className="pb-4">
            <h3 className="text-sm font-semibold text-gray-950">业务定义与使用说明</h3>
            <p className="mt-2 text-sm leading-6 text-gray-600">{definition.description || "尚未填写。请说明这个数据集包含什么、哪些问题应优先使用、何时可以直接查询原始来源，以及不适用的场景。"}</p>
            {definition.tags?.length ? <div className="mt-3 flex flex-wrap gap-2">{definition.tags.map((tag) => <span key={tag} className="rounded-full bg-[#002fa7]/[0.07] px-2.5 py-1 text-xs text-[#00246f]">{tag}</span>)}</div> : null}
          </section>
          <section className="border-y border-black/[0.06] py-4">
            <h3 className="text-sm font-semibold text-gray-950">字段契约</h3>
            <p className="mt-1 text-xs text-gray-500">查询时每张来源都会按此字段集合对齐，并自动附加行级溯源字段。</p>
            <div className="mt-3 flex flex-wrap gap-2">{fields.map((field) => <span key={field} className="rounded-lg bg-slate-100 px-2.5 py-1 text-xs text-slate-700">{field}</span>)}</div>
          </section>
          <section className="border-b border-black/[0.06] pb-4">
            <h3 className="text-sm font-semibold text-gray-950">已登记来源</h3>
            <div className="mt-3 divide-y divide-black/[0.05] rounded-lg border border-black/[0.08]">
              {sources.map((source) => <div key={source.asset_id} className="flex items-center justify-between gap-4 px-4 py-3"><div className="min-w-0"><p className="truncate text-sm font-medium text-gray-800">{source.name || source.asset_id}</p><p className="mt-1 text-xs text-gray-400">{source.sheet_name || "默认表"} · {source.fields?.length ?? 0} 列</p></div><span className="shrink-0 text-xs text-gray-500">{typeof source.rows_estimate === "number" ? `${source.rows_estimate.toLocaleString()} 行` : "行数待 Profile"}</span></div>)}
            </div>
          </section>
          <section className="border-b border-black/[0.06] pb-4"><h3 className="text-sm font-semibold text-gray-950">时间覆盖</h3>{coverage.length ? <div className="mt-3 space-y-2">{coverage.map((entry, index) => <div key={`${String(entry.source_asset_id ?? index)}`} className="rounded-lg border border-black/[0.06] px-3 py-2 text-xs text-gray-600"><span className="font-semibold text-gray-800">{String(entry.source_name ?? entry.source_asset_id ?? "来源")}</span>{Array.isArray(entry.dimensions) ? entry.dimensions.map((dimension: any) => <p key={`${dimension.field}-${dimension.min}`} className="mt-1">{dimension.field}：{dimension.min} 至 {dimension.max} <span className="text-gray-400">（{dimension.basis === "profile_sample" ? "Profile 样本" : dimension.basis}）</span></p>) : null}</div>)}</div> : <p className="mt-2 text-sm text-gray-500">尚无可确认的时间字段覆盖。点击“更新 Profile”后，系统会从来源 Profile 汇总可识别的时间范围。</p>}</section>
          <details className="rounded-lg border border-black/[0.08] p-4"><summary className="cursor-pointer text-sm font-semibold text-[#002fa7]">查看原始 dataset.json</summary><pre className="mt-3 max-h-72 overflow-auto rounded-lg bg-gray-950 p-4 text-xs leading-5 text-gray-100">{JSON.stringify(definition, null, 2)}</pre></details>
        </div>
        <footer className="flex justify-end border-t border-black/[0.06] px-6 py-4"><button type="button" onClick={onClose} className="h-10 rounded-xl border border-black/[0.08] px-4 text-sm font-semibold text-gray-700">关闭</button></footer>
      </section>
    </div>
  );
}

function LogicalDatasetDefinitionEditorModal({ asset, busy, onClose, onSave }: { asset: TableAsset; busy: boolean; onClose: () => void; onSave: (asset: TableAsset, payload: { name: string; description: string; tags: string[]; preferredIntents: string[]; directSourceAllowed: boolean }) => void }) {
  const definition = asset.logical_dataset!;
  const [name, setName] = useState(asset.file_name);
  const [description, setDescription] = useState(definition.description || "");
  const [tagsText, setTagsText] = useState((definition.tags || []).join(", "));
  return <div className="fixed inset-0 z-[160] flex items-center justify-center bg-black/40 px-4 py-6 backdrop-blur-sm"><section className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-[20px] bg-white shadow-2xl" role="dialog" aria-modal="true" aria-label="编辑逻辑数据集定义"><header className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5"><div><p className="text-sm font-semibold text-[#002fa7]">编辑逻辑数据集定义</p><p className="mt-1 text-sm text-gray-500">填写业务定义和使用边界；不改变来源表、字段契约或原始数据。</p></div><button type="button" onClick={onClose} disabled={busy} className="rounded-full p-2 text-gray-400 hover:bg-black/[0.04]"><X className="h-5 w-5" /></button></header><div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-6 py-5"><label className="block text-sm font-semibold text-slate-700">名称<input value={name} onChange={(event) => setName(event.target.value)} className="mt-2 h-11 w-full rounded-xl border border-black/[0.1] px-3 text-sm font-normal outline-none focus:border-[#002fa7]" /></label><label className="block text-sm font-semibold text-slate-700">业务定义与使用说明<textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="说明数据集包含什么；什么问题应优先使用它；何时可直接查询原始来源；哪些场景不适用。" className="mt-2 min-h-32 w-full rounded-xl border border-black/[0.1] px-3 py-2 text-sm font-normal outline-none focus:border-[#002fa7]" /></label><label className="block text-sm font-semibold text-slate-700">标签<input value={tagsText} onChange={(event) => setTagsText(event.target.value)} placeholder="销量, 上险量, 月度" className="mt-2 h-11 w-full rounded-xl border border-black/[0.1] px-3 text-sm font-normal outline-none focus:border-[#002fa7]" /></label><p className="rounded-xl border border-[#002fa7]/15 bg-[#002fa7]/[0.03] px-3 py-2 text-xs leading-5 text-[#00246f]">系统保留通用的跨来源分析默认策略；Agent 会优先读取上述说明判断何时使用此数据集，不需要维护不断膨胀的路由选项。</p></div><footer className="flex justify-end gap-2 border-t border-black/[0.06] px-6 py-4"><button type="button" onClick={onClose} disabled={busy} className="h-10 rounded-xl border border-black/[0.08] px-4 text-sm font-semibold text-gray-700">取消</button><button type="button" disabled={busy || !name.trim()} onClick={() => onSave(asset, { name: name.trim(), description, tags: tagsText.split(/[,，]/).map((item) => item.trim()).filter(Boolean), preferredIntents: definition.routing?.preferred_intents || [], directSourceAllowed: definition.routing?.direct_source_allowed !== false })} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:opacity-50">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}保存定义</button></footer></section></div>;
}

function DefinitionStat({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg bg-slate-50 px-4 py-3"><p className="text-xs text-gray-500">{label}</p><p className="mt-1 text-sm font-semibold text-gray-900">{value}</p></div>;
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
