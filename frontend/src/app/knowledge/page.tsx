"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  Database,
  FileText,
  BookOpenCheck,
  FileUp,
  FolderOpen,
  Loader2,
  RefreshCw,
  Settings,
  Table2,
  X,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import KnowledgeOverview from "@/components/knowledge/KnowledgeOverview";
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import { useApp } from "@/lib/store";
import {
  getKnowledgeFileTree,
  getKnowledgeStatus,
  listKnowledgeDatabaseSourceTables,
  listKnowledgeDatabaseSources,
  listKnowledgeDocuments,
  listKnowledgeImportJobs,
  listKnowledgeFiles,
  listTableAssets,
  previewKnowledgeFile,
  publishKnowledgeDocumentVector,
  snapshotKnowledgeFileToLlmWikiRaw,
  createKnowledgeImportJob,
  saveKnowledgeDatabaseSource,
  testKnowledgeDatabaseSource,
  type KnowledgeDatabaseSource,
  type KnowledgeDirectoryFile,
  type KnowledgeDocument,
  type KnowledgeFilePreview,
  type KnowledgeImportJob,
  type KnowledgeStatus,
  type KnowledgeTreeNode,
  type TableAsset,
} from "@/lib/api";

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
  if (error instanceof Error) return error.message;
  return String(error || "未知错误");
}

function jobStatusLabel(job: KnowledgeImportJob): string {
  if (job.status === "queued") return "排队中";
  if (job.status === "running") return "处理中";
  if (job.status === "succeeded") return "已完成";
  if (job.status === "failed") return "失败";
  if (job.status === "cancelled") return "已取消";
  return job.status;
}

function jobStatusClass(job: KnowledgeImportJob): string {
  if (job.status === "succeeded") return "bg-emerald-50 text-emerald-700";
  if (job.status === "failed") return "bg-red-50 text-red-600";
  if (job.status === "running") return "bg-[#002fa7]/10 text-[#002fa7]";
  return "bg-gray-100 text-gray-600";
}

function isVectorPublishJob(job: KnowledgeImportJob): boolean {
  return job.metadata?.kind === "vector_publish" || job.file_type === "vector";
}

function isLlmWikiJob(job: KnowledgeImportJob): boolean {
  return job.metadata?.kind === "llm_wiki_ingest" || job.file_type === "llm_wiki";
}

function jobKindLabel(job: KnowledgeImportJob): string {
  if (isLlmWikiJob(job)) return "Wiki 编译";
  return isVectorPublishJob(job) ? "向量导入" : "文件导入";
}

type AssetView = "files" | "tables" | "databases";
const HOME_JOB_PAGE_SIZE = 3;

function tableAssetLabel(asset: TableAsset): string {
  if (asset.source_type === "excel") return `Excel${asset.sheet_name ? ` · ${asset.sheet_name}` : ""}`;
  return asset.source_type.toUpperCase();
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

function KnowledgeFileTree({
  node,
  depth = 0,
  expandedPaths,
  selectedPath,
  onToggle,
  onPreview,
}: {
  node: KnowledgeTreeNode;
  depth?: number;
  expandedPaths: Set<string>;
  selectedPath?: string | null;
  onToggle: (path: string) => void;
  onPreview: (node: KnowledgeTreeNode) => void;
}) {
  const children = node.children ?? [];
  const isRoot = depth === 0;
  const isExpanded = isRoot || expandedPaths.has(node.virtual_path);

  if (node.type === "file") {
    const selected = selectedPath === node.virtual_path;
    return (
      <button
        type="button"
        onClick={() => onPreview(node)}
        className={`group flex w-full items-center gap-1.5 rounded-xl px-2 py-1.5 text-left text-[12px] transition ${
          selected ? "bg-[#002fa7]/10 text-[#002fa7]" : "text-gray-700 hover:bg-[#002fa7]/[0.05]"
        }`}
        style={{ paddingLeft: `${Math.min(depth, 5) * 10 + 8}px` }}
        title={node.storage_path}
      >
        <span aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
        <FileText className="h-3.5 w-3.5 shrink-0 text-[#002fa7]/75" />
        <span className="min-w-0 flex-1 truncate">{node.name}</span>
        {typeof node.size_bytes === "number" ? (
          <span className="shrink-0 text-[10px] text-gray-400">{formatBytes(node.size_bytes)}</span>
        ) : null}
      </button>
    );
  }

  return (
    <div>
      {!isRoot ? (
        <button
          type="button"
          onClick={() => onToggle(node.virtual_path)}
          className="flex w-full items-center gap-1.5 rounded-xl px-2 py-1.5 text-left text-[12px] font-medium text-gray-800 transition hover:bg-black/[0.035]"
          style={{ paddingLeft: `${Math.min(depth, 5) * 10 + 8}px` }}
          title={node.storage_path}
        >
          {isExpanded ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-gray-400" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-gray-400" />
          )}
          <FolderOpen className="h-3.5 w-3.5 shrink-0 text-[#002fa7]/75" />
          <span className="min-w-0 flex-1 truncate">{node.name}</span>
        </button>
      ) : null}
      {children.length > 0 && isExpanded ? (
        <div className="space-y-0.5">
          {children.map((child) => (
            <KnowledgeFileTree
              key={child.storage_path || child.virtual_path}
              node={child}
              depth={depth + 1}
              expandedPaths={expandedPaths}
              selectedPath={selectedPath}
              onToggle={onToggle}
              onPreview={onPreview}
            />
          ))}
          {node.truncated ? (
            <div className="px-3 py-1 text-[11px] text-gray-400">还有更多文件，已折叠显示。</div>
          ) : null}
        </div>
      ) : isRoot ? (
        <div className="rounded-2xl border border-dashed border-black/[0.08] bg-white/60 px-3 py-6 text-center text-xs text-gray-400">
          目录里还没有文件。
        </div>
      ) : null}
    </div>
  );
}

export default function KnowledgePage() {
  const pathname = usePathname();
  const {
    sidebarOpen,
    toggleSidebar,
    sidebarWidth,
    setSidebarWidth,
  } = useApp();
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [directoryFiles, setDirectoryFiles] = useState<KnowledgeDirectoryFile[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [importJobs, setImportJobs] = useState<KnowledgeImportJob[]>([]);
  const [tableAssets, setTableAssets] = useState<TableAsset[]>([]);
  const [databaseSources, setDatabaseSources] = useState<KnowledgeDatabaseSource[]>([]);
  const [assetView, setAssetView] = useState<AssetView>("files");
  const [fileTree, setFileTree] = useState<KnowledgeTreeNode | null>(null);
  const [expandedTreePaths, setExpandedTreePaths] = useState<Set<string>>(new Set());
  const [previewFile, setPreviewFile] = useState<KnowledgeFilePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const previewRequestRef = useRef(0);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadTitle, setUploadTitle] = useState("");
  const [includeInWikiRaw, setIncludeInWikiRaw] = useState(false);
  const [loading, setLoading] = useState(true);
  const [uploadingDocument, setUploadingDocument] = useState(false);
  const [snapshottingRaw, setSnapshottingRaw] = useState(false);
  const [rebuildingDocumentId, setRebuildingDocumentId] = useState<string | null>(null);
  const [jobPage, setJobPage] = useState(1);
  const [databaseModalOpen, setDatabaseModalOpen] = useState(false);
  const [databaseDraft, setDatabaseDraft] = useState<KnowledgeDatabaseSource>(() => emptyDatabaseSource());
  const [databaseTables, setDatabaseTables] = useState<string[]>([]);
  const [databaseBusy, setDatabaseBusy] = useState(false);
  const [databaseModalStatus, setDatabaseModalStatus] = useState<{
    type: "success" | "error" | "info";
    message: string;
  } | null>(null);
  const [mounted, setMounted] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    if (!databaseModalStatus) return;
    const timer = window.setTimeout(() => setDatabaseModalStatus(null), 3000);
    return () => window.clearTimeout(timer);
  }, [databaseModalStatus]);

  const handleSidebarResize = useCallback(
    (delta: number) => {
      setSidebarWidth((prev: number) => Math.max(200, prev + delta));
    },
    [setSidebarWidth]
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setToast(null);
    try {
      const nextStatus = await getKnowledgeStatus();
      setStatus(nextStatus);
      const [nextFiles, nextTree, nextTables, nextDatabaseSources, nextDocuments] = await Promise.all([
        listKnowledgeFiles(),
        getKnowledgeFileTree(),
        listTableAssets(false),
        listKnowledgeDatabaseSources(),
        listKnowledgeDocuments(),
      ]);
      setDirectoryFiles(nextFiles);
      setFileTree(nextTree);
      setTableAssets(nextTables);
      setDatabaseSources(nextDatabaseSources);
      setDocuments(nextDocuments);
      const nextJobs = await listKnowledgeImportJobs();
      setImportJobs(nextJobs);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!importJobs.some((job) => job.status === "queued" || job.status === "running")) return;
    const timer = window.setInterval(() => {
      refresh();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [importJobs, refresh]);

  const jobPageCount = Math.max(1, Math.ceil(importJobs.length / HOME_JOB_PAGE_SIZE));
  const previewDocument = previewFile
    ? documents.find((document) => document.virtual_path === previewFile.virtual_path) ?? null
    : null;
  const activePreviewVectorJob = previewDocument
    ? importJobs.find(
        (job) =>
          isVectorPublishJob(job) &&
          job.document_id === previewDocument.id &&
          (job.status === "queued" || job.status === "running")
      )
    : null;
  const pagedImportJobs = useMemo(
    () => importJobs.slice((jobPage - 1) * HOME_JOB_PAGE_SIZE, jobPage * HOME_JOB_PAGE_SIZE),
    [importJobs, jobPage]
  );

  useEffect(() => {
    setJobPage((page) => Math.min(page, jobPageCount));
  }, [jobPageCount]);

  const toggleTreePath = useCallback((path: string) => {
    setExpandedTreePaths((current) => {
      const next = new Set(current);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }, []);

  const previewTreeFile = useCallback(async (node: KnowledgeTreeNode) => {
    if (node.type !== "file") return;
    const requestId = ++previewRequestRef.current;
    setPreviewLoading(true);
    setToast(null);
    try {
      const nextPreview = await previewKnowledgeFile(node.virtual_path);
      if (previewRequestRef.current === requestId) setPreviewFile(nextPreview);
    } catch (error) {
      if (previewRequestRef.current === requestId) {
        setToast({ type: "error", message: errorMessage(error) });
      }
    } finally {
      if (previewRequestRef.current === requestId) setPreviewLoading(false);
    }
  }, []);

  const uploadDocument = useCallback(async () => {
    if (!selectedFile) {
      setToast({ type: "error", message: "请先选择一个文件。" });
      return;
    }
    setUploadingDocument(true);
    setToast(null);
    try {
      const publishTargets = ["local_markdown"];
      if (
        includeInWikiRaw &&
        [".md", ".markdown"].some((suffix) => selectedFile.name.toLowerCase().endsWith(suffix))
      ) {
        publishTargets.push("llm_wiki_raw");
      }
      const job = await createKnowledgeImportJob(
        selectedFile,
        uploadTitle.trim() || undefined,
        publishTargets
      );
      setSelectedFile(null);
      setIncludeInWikiRaw(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setUploadTitle("");
      setToast({ type: "success", message: `已加入导入队列：${job.file_name}` });
      await refresh();
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setUploadingDocument(false);
    }
  }, [includeInWikiRaw, refresh, selectedFile, uploadTitle]);

  const clearSelectedFile = useCallback(() => {
    setSelectedFile(null);
    setIncludeInWikiRaw(false);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, []);

  const snapshotPreviewToRaw = useCallback(async () => {
    if (!previewFile) return;
    const virtualPath = previewFile.virtual_path;
    setSnapshottingRaw(true);
    setToast(null);
    try {
      await snapshotKnowledgeFileToLlmWikiRaw(virtualPath);
      const nextPreview = await previewKnowledgeFile(virtualPath);
      setPreviewFile((current) => current?.virtual_path === virtualPath ? nextPreview : current);
      setToast({ type: "success", message: "已复制到 LLM Wiki Raw，可前往 Studio 编译。" });
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setSnapshottingRaw(false);
    }
  }, [previewFile]);

  const rebuildPreviewVectorIndex = useCallback(async () => {
    if (!previewDocument) return;
    setRebuildingDocumentId(previewDocument.id);
    setToast(null);
    try {
      const result = await publishKnowledgeDocumentVector(previewDocument.id);
      await refresh();
      setToast({
        type: "success",
        message: result.queued ? `已加入索引重建队列：${previewDocument.title}` : "该文档正在重建索引。",
      });
      setJobPage(1);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setRebuildingDocumentId(null);
    }
  }, [previewDocument, refresh]);

  const openDatabaseSourceModal = useCallback((source?: KnowledgeDatabaseSource) => {
    setDatabaseDraft(source ? { ...source, password: "" } : emptyDatabaseSource());
    setDatabaseTables(source?.selected_tables ?? []);
    setDatabaseModalStatus(null);
    setDatabaseModalOpen(true);
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
      setDatabaseModalStatus(null);
      setToast({ type: "success", message: `已保存数据源：${saved.name}` });
      await refresh();
    } catch (error) {
      setDatabaseModalStatus({ type: "error", message: errorMessage(error) });
    } finally {
      setDatabaseBusy(false);
    }
  }, [databaseDraft, refresh]);

  return (
    <div className="h-screen app-bg text-gray-900">
      <div className="fixed left-3 top-3 z-[80]">
        <Navbar
          sidebarOpen={sidebarOpen}
          toggleSidebar={toggleSidebar}
          showPanelToggles
          compact
        />
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

        {mounted && sidebarOpen && (
          <ResizeHandle onResize={handleSidebarResize} direction="left" />
        )}

        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto">
            <div className="workspace-page-container flex flex-col gap-5">
        {pathname === "/knowledge" ? (
          <KnowledgeOverview
            documentCount={documents.length}
            fileCount={directoryFiles.length}
            jobs={importJobs}
            loading={loading}
            onRefresh={refresh}
          />
        ) : (
          <>
        <KnowledgeWorkspaceHeader
          section="library"
          actions={
            <>
            <Link
              href="/settings?category=knowledge"
              className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full bg-[#002fa7]/10 px-3.5 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/15"
            >
              <Settings className="h-4 w-4" />
              知识库设置
            </Link>
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border border-black/[0.08] bg-white px-3.5 text-xs font-semibold text-gray-600 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
              刷新
            </button>
            </>
          }
        />
        <KnowledgeWorkspaceNav />

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

        <section className="grid min-h-[430px] gap-5 lg:grid-cols-[300px_1fr]">
          <aside className="flex min-h-0 flex-col rounded-[28px] border border-black/[0.06] bg-white/88 p-4 shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2.5">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.08] text-[#002fa7]">
                  <FolderOpen className="h-4.5 w-4.5" />
                </div>
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-gray-950">知识库目录</h2>
                  <p className="mt-0.5 text-[11px] text-gray-500">
                    {directoryFiles.length} 个文件
                  </p>
                </div>
              </div>
              <Link
                href="/settings?category=knowledge"
                className="rounded-xl p-2 text-gray-400 transition hover:bg-black/[0.04] hover:text-[#002fa7]"
                title="知识库设置"
              >
                <Settings className="h-4 w-4" />
              </Link>
            </div>

            <div className="mt-4 rounded-2xl bg-black/[0.025] p-3">
              <p className="text-[10px] font-semibold tracking-wide text-gray-400">目录</p>
              <p
                className="mt-2 whitespace-normal break-words text-[12px] leading-5 text-gray-700 [overflow-wrap:anywhere]"
                title={status?.local_markdown.physical_path || "backend/knowledge"}
              >
                {status?.local_markdown.physical_path || "backend/knowledge"}
              </p>
            </div>

            <div className="mt-4 flex rounded-2xl bg-black/[0.035] p-1 text-[11px] font-semibold text-gray-500">
              {[
                { key: "files" as const, label: "文件", count: directoryFiles.length, icon: FileText },
                { key: "tables" as const, label: "表格", count: tableAssets.length, icon: Table2 },
                { key: "databases" as const, label: "数据库", count: databaseSources.length, icon: Database },
              ].map((item) => {
                const Icon = item.icon;
                const active = assetView === item.key;
                return (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => setAssetView(item.key)}
                    className={`flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-xl px-2 py-2 transition ${
                      active ? "bg-white text-[#002fa7] shadow-sm" : "hover:bg-white/60"
                    }`}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    <span>{item.label}</span>
                    <span className={active ? "text-[#002fa7]/70" : "text-gray-400"}>{item.count}</span>
                  </button>
                );
              })}
            </div>

            <div className="mt-4 min-h-0 flex-1">
              {assetView === "files" ? (
                <>
                  <div className="mb-2 flex items-center justify-between">
                    <p className="text-[11px] font-semibold text-gray-500">文件树</p>
                    <span className="text-[10px] text-gray-400">{directoryFiles.length}</span>
                  </div>
                  <div className="max-h-[280px] overflow-y-auto rounded-2xl bg-black/[0.018] px-1 py-2 pr-1">
                    {fileTree && (fileTree.children?.length ?? 0) > 0 ? (
                      <KnowledgeFileTree
                        node={fileTree}
                        expandedPaths={expandedTreePaths}
                        selectedPath={previewFile?.virtual_path}
                        onToggle={toggleTreePath}
                        onPreview={previewTreeFile}
                      />
                    ) : (
                      <div className="rounded-2xl border border-dashed border-black/[0.08] bg-white/60 px-3 py-6 text-center text-xs text-gray-400">
                        目录里还没有文件。
                      </div>
                    )}
                  </div>
                </>
              ) : null}

              {assetView === "tables" ? (
                <div className="space-y-2">
                  <div className="mb-2 flex items-center justify-between">
                    <p className="text-[11px] font-semibold text-gray-500">表格资产</p>
                    <Link href="/analytics" className="text-[10px] font-medium text-[#002fa7]">
                      去问数
                    </Link>
                  </div>
                  {tableAssets.length > 0 ? (
                    tableAssets.slice(0, 30).map((asset) => (
                      <div key={asset.asset_id} className="rounded-2xl bg-black/[0.025] px-3 py-3">
                        <div className="flex items-start gap-2">
                          <Table2 className="mt-0.5 h-4 w-4 shrink-0 text-[#002fa7]" />
                          <div className="min-w-0">
                            <p className="truncate text-xs font-semibold text-gray-900" title={asset.file_name}>
                              {asset.file_name}
                            </p>
                            <p className="mt-1 text-[11px] text-gray-400">{tableAssetLabel(asset)}</p>
                          </div>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="rounded-2xl border border-dashed border-black/[0.08] bg-white/60 px-3 py-6 text-center text-xs text-gray-400">
                      上传 Excel / CSV 后会出现在这里。
                    </div>
                  )}
                </div>
              ) : null}

              {assetView === "databases" ? (
                <div className="space-y-2">
                  <div className="mb-2 flex items-center justify-between">
                    <p className="text-[11px] font-semibold text-gray-500">数据库</p>
                    <button
                      type="button"
                      onClick={() => openDatabaseSourceModal()}
                      className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-[10px] font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/15"
                    >
                      添加
                    </button>
                  </div>
                  {databaseSources.map((source) => (
                    <button
                      key={source.id}
                      type="button"
                      onClick={() => openDatabaseSourceModal(source)}
                      className="w-full rounded-2xl bg-black/[0.025] px-3 py-3 text-left transition hover:bg-[#002fa7]/[0.05]"
                    >
                      <div className="flex items-start gap-2">
                        <Database className="mt-0.5 h-4 w-4 shrink-0 text-[#002fa7]" />
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-center gap-1.5">
                            <p className="truncate text-xs font-semibold text-gray-900">{source.name}</p>
                            {source.builtin ? (
                              <span className="shrink-0 rounded-full bg-emerald-50 px-1.5 py-0.5 text-[9px] font-medium text-emerald-700">
                                默认
                              </span>
                            ) : null}
                          </div>
                          <p className="mt-1 truncate text-[11px] text-gray-400">
                            {source.host}:{source.port}/{source.database}
                          </p>
                          <p className="mt-1 text-[10px] text-gray-400">已选 {source.selected_tables.length} 张表</p>
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          </aside>

          <section className="flex min-h-[430px] flex-col overflow-hidden rounded-[32px] border border-black/[0.06] bg-white p-8 shadow-sm">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-2xl font-semibold tracking-tight text-gray-950">
                  {previewFile || previewLoading ? "文件详情" : "智能导入"}
                </h2>
              </div>
            </div>

            {previewFile || previewLoading ? (
              <div className="mt-8 flex flex-1 flex-col overflow-hidden rounded-[28px] bg-black/[0.018]">
                <div className="flex flex-col gap-3 border-b border-black/[0.05] px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <p className="truncate text-base font-semibold text-gray-950">
                      {previewLoading ? "正在打开文件..." : previewFile?.name}
                    </p>
                    {previewFile ? (
                      <p className="mt-1 truncate text-xs text-gray-400">
                        {formatBytes(previewFile.size_bytes)} · {previewFile.virtual_path}
                      </p>
                    ) : null}
                  </div>
                  <div className="flex flex-wrap items-center gap-2 sm:shrink-0 sm:justify-end">
                    {previewDocument?.source_type === "pdf_mineru" ? (
                      <button
                        type="button"
                        onClick={rebuildPreviewVectorIndex}
                        disabled={rebuildingDocumentId === previewDocument.id || Boolean(activePreviewVectorJob)}
                        title="仅重建该 PDF 的文本、BM25 与图片向量索引"
                        className="inline-flex h-9 items-center gap-2 rounded-full border border-[#002fa7]/15 bg-white px-4 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/[0.04] disabled:cursor-wait disabled:opacity-60"
                      >
                        {rebuildingDocumentId === previewDocument.id || activePreviewVectorJob ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Database className="h-3.5 w-3.5" />
                        )}
                        {activePreviewVectorJob ? "索引重建中" : "重建向量索引"}
                      </button>
                    ) : null}
                    {previewFile && [".md", ".markdown"].some((suffix) => previewFile.name.toLowerCase().endsWith(suffix)) ? (
                      <button
                        type="button"
                        onClick={snapshotPreviewToRaw}
                        disabled={snapshottingRaw || previewFile.llm_wiki_raw?.available}
                        className="inline-flex h-9 items-center gap-2 rounded-full bg-[#002fa7] px-4 text-xs font-semibold text-white transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:bg-emerald-50 disabled:text-emerald-700"
                      >
                        {snapshottingRaw ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BookOpenCheck className="h-3.5 w-3.5" />}
                        {previewFile.llm_wiki_raw?.available
                          ? "已在 Raw"
                          : previewFile.llm_wiki_raw?.changed_since_snapshot
                            ? "更新 Raw 快照"
                            : "加入 LLM Wiki Raw"}
                      </button>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => {
                        previewRequestRef.current += 1;
                        setPreviewLoading(false);
                        setPreviewFile(null);
                      }}
                      title="关闭文件详情"
                      aria-label="关闭文件详情"
                      className="flex h-9 w-9 items-center justify-center rounded-full text-gray-500 transition hover:bg-white hover:text-gray-900"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                {previewLoading ? (
                  <div className="flex min-h-[260px] flex-1 items-center justify-center text-sm text-gray-400">
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    加载预览中
                  </div>
                ) : previewFile?.preview_type === "text" ? (
                  <pre className="max-h-[520px] flex-1 overflow-auto whitespace-pre-wrap break-words px-5 py-5 text-left text-xs leading-6 text-gray-700">
                    {previewFile.content || "这个文件是空的。"}
                    {previewFile.truncated ? "\n\n……文件较大，只显示前面一部分。" : ""}
                  </pre>
                ) : (
                  <div className="flex min-h-[260px] flex-1 items-center justify-center px-6 text-center text-sm text-gray-500">
                    {previewFile?.message || "这个文件暂时不能直接预览。"}
                  </div>
                )}
              </div>
            ) : (
            <div className="mt-8 flex flex-1 flex-col items-center justify-center rounded-[28px] border border-dashed border-[#002fa7]/25 bg-white px-6 py-10 text-center">
              <div className="flex h-16 w-16 items-center justify-center rounded-3xl bg-[#002fa7]/[0.08] text-[#002fa7] shadow-sm">
                {uploadingDocument ? <Loader2 className="h-8 w-8 animate-spin" /> : <FileUp className="h-8 w-8" />}
              </div>
              <h3 className="mt-5 text-lg font-semibold text-gray-950">上传到知识库</h3>
              <p className="mt-2 max-w-md text-sm leading-6 text-gray-500">
                选择 PDF 或 Markdown 文件。Excel / CSV 后续会在同一入口接入 Pandas Engine。
              </p>

              <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
                <label className="inline-flex h-11 cursor-pointer items-center gap-2 rounded-2xl bg-[#002fa7] px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-[#001f7a]">
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept="application/pdf,.pdf,text/markdown,.md,.markdown,.xlsx,.xls,.csv,.tsv,.txt,.docx"
                    className="hidden"
                    onChange={(event) => {
                      setSelectedFile(event.target.files?.[0] ?? null);
                      setIncludeInWikiRaw(false);
                    }}
                  />
                  <FileUp className="h-4 w-4" />
                  选择文件
                </label>
                <input
                  value={uploadTitle}
                  onChange={(event) => setUploadTitle(event.target.value)}
                  placeholder="可选标题"
                  className="h-11 w-64 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                />
                <button
                  type="button"
                  onClick={uploadDocument}
                  disabled={uploadingDocument || !selectedFile}
                  className="inline-flex h-11 items-center justify-center gap-2 rounded-2xl border border-black/[0.08] bg-gray-950 px-5 text-sm font-semibold text-white transition hover:bg-black disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {uploadingDocument ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                  导入知识库
                </button>
              </div>

              {selectedFile ? (
                <div className="mt-5 flex flex-col items-center gap-3">
                  <div className="inline-flex max-w-full items-center gap-2 rounded-2xl bg-[#002fa7]/[0.06] px-4 py-2 text-sm text-[#002fa7]">
                    <span className="min-w-0 truncate">
                      已选择：{selectedFile.name} · {formatBytes(selectedFile.size)}
                    </span>
                    <button
                      type="button"
                      onClick={clearSelectedFile}
                      disabled={uploadingDocument}
                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[#002fa7]/70 transition hover:bg-[#002fa7]/10 hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-45"
                      title="移除已选择文件"
                      aria-label="移除已选择文件"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  {[".md", ".markdown"].some((suffix) => selectedFile.name.toLowerCase().endsWith(suffix)) ? (
                    <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-gray-600">
                      <input
                        type="checkbox"
                        checked={includeInWikiRaw}
                        onChange={(event) => setIncludeInWikiRaw(event.target.checked)}
                        className="h-4 w-4 rounded border-gray-300 text-[#002fa7] focus:ring-[#002fa7]"
                      />
                      同时复制到 LLM Wiki Raw
                    </label>
                  ) : null}
                </div>
              ) : (
                <p className="mt-5 text-xs text-gray-400">选择文件后，小爪子会自动处理。</p>
              )}
            </div>
            )}

            <div className="mt-5 rounded-[24px] border border-black/[0.06] bg-black/[0.018] p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-gray-950">任务队列</h3>
                  <p className="mt-1 text-xs text-gray-400">
                    Wiki {importJobs.filter(isLlmWikiJob).length} 条 · 文件 {importJobs.filter((job) => !isVectorPublishJob(job) && !isLlmWikiJob(job)).length} 条 · 向量{" "}
                    {importJobs.filter(isVectorPublishJob).length} 条
                  </p>
                </div>
                <Link
                  href="/knowledge/imports"
                  className="text-xs font-medium text-[#002fa7] transition hover:text-[#001f7a]"
                >
                  查看队列
                </Link>
              </div>
              <div className="mt-3 space-y-2">
                {importJobs.length > 0 ? (
                  pagedImportJobs.map((job) => (
                    <Link
                      key={job.id}
                      href={`/knowledge/imports/${job.id}`}
                      className="block rounded-2xl bg-white px-3.5 py-3 shadow-sm ring-1 ring-black/[0.04] transition hover:bg-[#002fa7]/[0.025] hover:ring-[#002fa7]/20"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-center gap-2">
                            {isLlmWikiJob(job) ? (
                              <BookOpenCheck className="h-4 w-4 shrink-0 text-violet-600" />
                            ) : isVectorPublishJob(job) ? (
                              <Database className="h-4 w-4 shrink-0 text-[#002fa7]" />
                            ) : (
                              <FileText className="h-4 w-4 shrink-0 text-emerald-600" />
                            )}
                            <p className="truncate text-sm font-medium text-gray-900" title={job.file_name}>
                              {job.title || job.file_name}
                            </p>
                          </div>
                          <p className="mt-1 text-xs text-gray-400">
                            {jobKindLabel(job)} · {job.current_step || job.status} · {formatBytes(job.file_size)}
                          </p>
                        </div>
                        <span className={`shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium ${jobStatusClass(job)}`}>
                          {jobStatusLabel(job)}
                        </span>
                      </div>
                      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-gray-100">
                        <div
                          className={`h-full rounded-full ${job.status === "failed" ? "bg-red-500" : "bg-[#002fa7]"}`}
                          style={{ width: `${Math.max(0, Math.min(100, job.progress || 0))}%` }}
                        />
                      </div>
                      {job.error_message ? (
                        <p className="mt-2 line-clamp-2 text-xs text-red-500" title={job.error_message}>
                          {job.error_message}
                        </p>
                      ) : null}
                    </Link>
                  ))
                ) : (
                  <div className="rounded-2xl border border-dashed border-black/[0.08] bg-white/70 px-4 py-5 text-center text-xs text-gray-400">
                    还没有导入任务。
                  </div>
                )}
              </div>
              {importJobs.length > HOME_JOB_PAGE_SIZE ? (
                <div className="mt-3 flex items-center justify-between gap-3 text-xs text-gray-400">
                  <span>
                    第 {jobPage} / {jobPageCount} 页 · 共 {importJobs.length} 条
                  </span>
                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => setJobPage((page) => Math.max(1, page - 1))}
                      disabled={jobPage <= 1}
                      className="h-8 rounded-full border border-black/[0.06] bg-white px-3 font-medium text-gray-600 transition hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      上一页
                    </button>
                    <button
                      type="button"
                      onClick={() => setJobPage((page) => Math.min(jobPageCount, page + 1))}
                      disabled={jobPage >= jobPageCount}
                      className="h-8 rounded-full border border-black/[0.06] bg-white px-3 font-medium text-gray-600 transition hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      下一页
                    </button>
                  </div>
                </div>
              ) : null}
            </div>

          </section>
        </section>

        {databaseModalOpen ? (
          <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/35 px-4 py-6 backdrop-blur-sm">
            <div className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
              <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
                <div>
                  <h3 className="text-lg font-semibold text-gray-950">
                    {databaseDraft.id ? "编辑数据库" : "添加数据库"}
                  </h3>
                  <p className="mt-1 text-sm text-gray-500">保存数据库连接和可用表，后续问数 Agent 会优先从这里选择数据。</p>
                </div>
                <button
                  type="button"
                  onClick={() => setDatabaseModalOpen(false)}
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
                      value={databaseDraft.name}
                      onChange={(event) => updateDatabaseDraft({ name: event.target.value })}
                      placeholder="例如：项目 PostgreSQL"
                      className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    />
                  </label>
                  <label className="space-y-1.5 md:col-span-2">
                    <span className="text-xs font-semibold text-gray-500">描述</span>
                    <input
                      value={databaseDraft.description || ""}
                      onChange={(event) => updateDatabaseDraft({ description: event.target.value })}
                      placeholder="这组表主要用来分析什么"
                      className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    />
                  </label>
                  <label className="space-y-1.5">
                    <span className="text-xs font-semibold text-gray-500">Host</span>
                    <input
                      value={databaseDraft.host}
                      disabled={databaseDraft.id === "project_postgres"}
                      onChange={(event) => updateDatabaseDraft({ host: event.target.value })}
                      className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    />
                  </label>
                  <label className="space-y-1.5">
                    <span className="text-xs font-semibold text-gray-500">端口</span>
                    <input
                      type="number"
                      value={databaseDraft.port}
                      disabled={databaseDraft.id === "project_postgres"}
                      onChange={(event) => updateDatabaseDraft({ port: Number(event.target.value) || 5432 })}
                      className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    />
                  </label>
                  <label className="space-y-1.5">
                    <span className="text-xs font-semibold text-gray-500">数据库名</span>
                    <input
                      value={databaseDraft.database}
                      disabled={databaseDraft.id === "project_postgres"}
                      onChange={(event) => updateDatabaseDraft({ database: event.target.value })}
                      className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    />
                  </label>
                  <label className="space-y-1.5">
                    <span className="text-xs font-semibold text-gray-500">用户名</span>
                    <input
                      value={databaseDraft.username}
                      disabled={databaseDraft.id === "project_postgres"}
                      onChange={(event) => updateDatabaseDraft({ username: event.target.value })}
                      className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    />
                  </label>
                  <label className="space-y-1.5 md:col-span-2">
                    <span className="text-xs font-semibold text-gray-500">密码</span>
                    <input
                      type="password"
                      value={databaseDraft.password || ""}
                      disabled={databaseDraft.id === "project_postgres"}
                      onChange={(event) => updateDatabaseDraft({ password: event.target.value })}
                      placeholder={databaseDraft.password_configured ? "已配置，留空不修改" : "请输入密码"}
                      className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none transition disabled:bg-gray-50 disabled:text-gray-400 focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/[0.08]"
                    />
                  </label>
                </div>

                {databaseModalStatus ? (
                  <div
                    className={`mt-5 flex items-start gap-2 rounded-2xl border px-4 py-3 text-sm ${
                      databaseModalStatus.type === "success"
                        ? "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                        : databaseModalStatus.type === "error"
                          ? "border-red-500/15 bg-red-50 text-red-600"
                          : "border-[#002fa7]/15 bg-[#002fa7]/[0.05] text-[#002fa7]"
                    }`}
                  >
                    {databaseModalStatus.type === "success" ? (
                      <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                    ) : databaseModalStatus.type === "error" ? (
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                    ) : (
                      <Database className="mt-0.5 h-4 w-4 shrink-0" />
                    )}
                    <span className="break-words">{databaseModalStatus.message}</span>
                  </div>
                ) : null}

                <div className="mt-5 rounded-3xl border border-black/[0.06] bg-black/[0.018] p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="text-sm font-semibold text-gray-950">选择表</p>
                      <p className="mt-1 text-xs text-gray-400">只保存你希望 Agent 使用的表。</p>
                    </div>
                    <button
                      type="button"
                      onClick={loadDatabaseTables}
                      disabled={databaseBusy || !databaseDraft.id}
                      className="inline-flex h-9 items-center gap-2 rounded-full bg-white px-3 text-xs font-semibold text-[#002fa7] shadow-sm ring-1 ring-black/[0.05] transition hover:bg-[#002fa7]/[0.04] disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      {databaseBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                      读取表
                    </button>
                  </div>
                  <div className="mt-3 max-h-52 overflow-y-auto rounded-2xl bg-white p-2">
                    {databaseTables.length > 0 ? (
                      <div className="grid gap-1.5 sm:grid-cols-2">
                        {databaseTables.map((table) => {
                          const checked = databaseDraft.selected_tables.includes(table);
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
                                    ? [...databaseDraft.selected_tables, table]
                                    : databaseDraft.selected_tables.filter((item) => item !== table);
                                  updateDatabaseDraft({ selected_tables: next });
                                }}
                                className="h-3.5 w-3.5 accent-[#002fa7]"
                              />
                              <span className="min-w-0 truncate" title={table}>{table}</span>
                            </label>
                          );
                        })}
                      </div>
                    ) : (
                      <p className="px-3 py-6 text-center text-xs text-gray-400">
                        保存数据源后点击“读取表”，或先手动保存空表配置。
                      </p>
                    )}
                  </div>
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-end gap-3 border-t border-black/[0.06] px-6 py-4">
                <button
                  type="button"
                  onClick={testDatabaseDraft}
                  disabled={databaseBusy}
                  className="inline-flex h-10 items-center gap-2 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm font-semibold text-gray-700 transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
                >
                  {databaseBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
                  测试连接
                </button>
                <button
                  type="button"
                  onClick={() => setDatabaseModalOpen(false)}
                  className="h-10 rounded-2xl px-4 text-sm font-semibold text-gray-500 transition hover:bg-black/[0.04]"
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={saveDatabaseDraft}
                  disabled={databaseBusy || !databaseDraft.name.trim()}
                  className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {databaseBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                  保存
                </button>
              </div>
            </div>
          </div>
        ) : null}

          </>
        )}

            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
