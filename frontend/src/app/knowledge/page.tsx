"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  Database,
  FileText,
  FileUp,
  FolderOpen,
  Loader2,
  RefreshCw,
  Settings,
  X,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import { useApp } from "@/lib/store";
import {
  getKnowledgeFileTree,
  getKnowledgeStatus,
  listKnowledgeImportJobs,
  listKnowledgeFiles,
  previewKnowledgeFile,
  createKnowledgeImportJob,
  type KnowledgeDirectoryFile,
  type KnowledgeFilePreview,
  type KnowledgeImportJob,
  type KnowledgeStatus,
  type KnowledgeTreeNode,
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
  if (job.status === "running") return "导入中";
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

function jobKindLabel(job: KnowledgeImportJob): string {
  return isVectorPublishJob(job) ? "向量导入" : "文件导入";
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
        className={`group flex w-full items-center gap-2 rounded-xl px-2 py-1.5 text-left text-[12px] transition ${
          selected ? "bg-[#002fa7]/10 text-[#002fa7]" : "text-gray-700 hover:bg-[#002fa7]/[0.05]"
        }`}
        style={{ paddingLeft: `${Math.min(depth, 5) * 10 + 8}px` }}
        title={node.storage_path}
      >
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
          <FolderOpen className="h-3.5 w-3.5 shrink-0 text-amber-500" />
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
  const {
    sidebarOpen,
    toggleSidebar,
    sidebarWidth,
    setSidebarWidth,
  } = useApp();
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [directoryFiles, setDirectoryFiles] = useState<KnowledgeDirectoryFile[]>([]);
  const [importJobs, setImportJobs] = useState<KnowledgeImportJob[]>([]);
  const [fileTree, setFileTree] = useState<KnowledgeTreeNode | null>(null);
  const [expandedTreePaths, setExpandedTreePaths] = useState<Set<string>>(new Set());
  const [previewFile, setPreviewFile] = useState<KnowledgeFilePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadTitle, setUploadTitle] = useState("");
  const [loading, setLoading] = useState(true);
  const [uploadingDocument, setUploadingDocument] = useState(false);
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
      const [nextFiles, nextTree] = await Promise.all([
        listKnowledgeFiles(),
        getKnowledgeFileTree(),
      ]);
      setDirectoryFiles(nextFiles);
      setFileTree(nextTree);
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
    setPreviewLoading(true);
    setToast(null);
    try {
      const nextPreview = await previewKnowledgeFile(node.virtual_path);
      setPreviewFile(nextPreview);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setPreviewLoading(false);
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
      const job = await createKnowledgeImportJob(selectedFile, uploadTitle.trim() || undefined);
      setSelectedFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setUploadTitle("");
      setToast({ type: "success", message: `已加入导入队列：${job.file_name}` });
      await refresh();
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setUploadingDocument(false);
    }
  }, [refresh, selectedFile, uploadTitle]);

  const clearSelectedFile = useCallback(() => {
    setSelectedFile(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, []);

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
            <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 px-5 py-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-gray-950">知识库</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-500">
              当前打通 PDF/MinerU 与 Markdown 双管道：PDF 解析成 md 后写入 <code className="rounded bg-black/[0.04] px-1.5 py-0.5">/knowledge/imported/</code>，
              同时发布到本地知识库目录和多模态向量索引。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Link
              href="/settings?category=knowledge"
              className="inline-flex h-10 items-center gap-2 rounded-full bg-[#002fa7]/10 px-4 text-sm font-medium text-[#002fa7] transition hover:bg-[#002fa7]/15"
            >
              <Settings className="h-4 w-4" />
              知识库设置
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

            <div className="mt-4 min-h-0 flex-1">
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
            </div>
          </aside>

          <section className="flex min-h-[430px] flex-col overflow-hidden rounded-[32px] border border-black/[0.06] bg-white p-8 shadow-sm">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-2xl font-semibold tracking-tight text-gray-950">智能导入</h2>
              </div>
            </div>

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
                    onChange={(event) => setSelectedFile(event.target.files?.[0] ?? null)}
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
                <div className="mt-5 inline-flex max-w-full items-center gap-2 rounded-2xl bg-[#002fa7]/[0.06] px-4 py-2 text-sm text-[#002fa7]">
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
              ) : (
                <p className="mt-5 text-xs text-gray-400">选择文件后，小爪子会自动处理。</p>
              )}
            </div>

            <div className="mt-5 rounded-[24px] border border-black/[0.06] bg-black/[0.018] p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-gray-950">任务队列</h3>
                  <p className="mt-1 text-xs text-gray-400">
                    文件 {importJobs.filter((job) => !isVectorPublishJob(job)).length} 条 · 向量{" "}
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
                  importJobs.slice(0, 6).map((job) => (
                    <Link
                      key={job.id}
                      href={`/knowledge/imports/${job.id}`}
                      className="block rounded-2xl bg-white px-3.5 py-3 shadow-sm ring-1 ring-black/[0.04] transition hover:bg-[#002fa7]/[0.025] hover:ring-[#002fa7]/20"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-center gap-2">
                            {isVectorPublishJob(job) ? (
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
            </div>

            {previewFile || previewLoading ? (
              <div className="mt-5 overflow-hidden rounded-[24px] border border-black/[0.06] bg-black/[0.025]">
                <div className="flex items-center justify-between gap-3 border-b border-black/[0.05] px-4 py-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-gray-900">
                      {previewLoading ? "正在打开文件..." : previewFile?.name}
                    </p>
                    {previewFile ? (
                      <p className="mt-0.5 truncate text-xs text-gray-400">
                        {formatBytes(previewFile.size_bytes)} · {previewFile.virtual_path}
                      </p>
                    ) : null}
                  </div>
                  {previewFile ? (
                    <button
                      type="button"
                      onClick={() => setPreviewFile(null)}
                      className="shrink-0 rounded-full px-3 py-1.5 text-xs font-medium text-gray-500 transition hover:bg-white hover:text-gray-900"
                    >
                      关闭
                    </button>
                  ) : null}
                </div>
                {previewLoading ? (
                  <div className="flex min-h-[180px] items-center justify-center text-sm text-gray-400">
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    加载预览中
                  </div>
                ) : previewFile?.preview_type === "text" ? (
                  <pre className="max-h-[320px] overflow-auto whitespace-pre-wrap break-words px-4 py-4 text-left text-xs leading-5 text-gray-700">
                    {previewFile.content || "这个文件是空的。"}
                    {previewFile.truncated ? "\n\n……文件较大，只显示前面一部分。" : ""}
                  </pre>
                ) : (
                  <div className="flex min-h-[180px] items-center justify-center px-6 text-center text-sm text-gray-500">
                    {previewFile?.message || "这个文件暂时不能直接预览。"}
                  </div>
                )}
              </div>
            ) : null}
          </section>
        </section>

            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
