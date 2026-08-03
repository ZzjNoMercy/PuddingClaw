"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertCircle,
  ArrowLeft,
  Bot,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  Code2,
  DatabaseZap,
  FileCheck2,
  FileCode2,
  FileUp,
  Layers3,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import {
  compileLlmWikiGbrain,
  createLlmWikiIngestJob,
  getBrainSchemaBundle,
  getBrainSchemaCatalog,
  getLlmWikiWorkspaceStatus,
  initializeBrainSchema,
  lintLlmWiki,
  previewBrainCustomSchema,
  rebuildLlmWikiAgents,
  saveBrainCustomSchema,
  snapshotLlmWikiRaw,
  type BrainSchemaBundle,
  type BrainSchemaPreview,
  type GbrainLinkType,
  type GbrainPageType,
  type GbrainPrimitive,
  type GbrainSchemaCatalog,
  type GbrainSchemaCatalogPack,
  type GbrainSchemaPackManifest,
  type LlmWikiCompileResult,
  type LlmWikiLintResult,
  type LlmWikiWorkspaceStatus,
} from "@/lib/api";
import "@/lib/monaco-config";
import { useApp } from "@/lib/store";
import { ReorderButtons, StringListEditor } from "./advanced-schema-editor";
import { schemaFieldLabel } from "./schema-ui-labels";

const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-sm text-gray-400">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 加载 YAML 预览…
    </div>
  ),
});

type MainTab = "workflow" | "builtins" | "custom" | "resolved";
type RawTarget = "custom" | "resolved" | "parent" | "brain";
type PageTypeEditorTarget = { index: number | null; initial: GbrainPageType };
type LinkTypeEditorTarget = { index: number | null; initial: GbrainLinkType };
type AgentsPreviewMode = "rendered" | "source";

const PRIMITIVES: GbrainPrimitive[] = ["entity", "media", "temporal", "annotation", "concept"];
const inputClass =
  "h-9 w-full rounded-xl border border-black/[0.08] bg-white px-3 text-sm text-gray-800 outline-none transition focus:border-[#002fa7]/35 focus:ring-2 focus:ring-[#002fa7]/10";

function cloneManifest(value: GbrainSchemaPackManifest): GbrainSchemaPackManifest {
  return JSON.parse(JSON.stringify(value)) as GbrainSchemaPackManifest;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

function clonePageType(value: GbrainPageType): GbrainPageType {
  return JSON.parse(JSON.stringify(value)) as GbrainPageType;
}

function cloneLinkType(value: GbrainLinkType): GbrainLinkType {
  return JSON.parse(JSON.stringify(value)) as GbrainLinkType;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  const translated = schemaFieldLabel(label);
  return (
    <label className="block min-w-0">
      <span className="mb-1.5 flex items-baseline gap-2 text-xs font-medium text-gray-700">
        {translated || label}
        {translated ? <code className="font-mono text-[10px] font-normal text-gray-400">{label}</code> : null}
        {hint ? <span className="font-normal text-gray-400">{hint}</span> : null}
      </span>
      {children}
    </label>
  );
}

function EditorSection({
  title,
  description,
  count,
  onAdd,
  children,
}: {
  title: string;
  description: string;
  count: number;
  onAdd: () => void;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <section className="rounded-2xl border border-black/[0.06] bg-white">
      <div className="flex items-center">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex min-w-0 flex-1 items-center gap-3 px-4 py-3 text-left"
          aria-expanded={open}
        >
          {open ? <ChevronDown className="h-4 w-4 shrink-0 text-gray-400" /> : <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" />}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
              <span className="rounded-full bg-black/[0.045] px-2 py-0.5 text-[10px] text-gray-500">{count}</span>
            </div>
            <p className="mt-0.5 text-[11px] text-gray-400">{description}</p>
          </div>
        </button>
        <button
          type="button"
          onClick={onAdd}
          className="mr-4 inline-flex h-8 shrink-0 items-center gap-1 rounded-full bg-[#002fa7]/10 px-3 text-xs font-medium text-[#002fa7] hover:bg-[#002fa7]/15"
        >
          <Plus className="h-3.5 w-3.5" /> 添加
        </button>
      </div>
      {open ? <div className="space-y-3 border-t border-black/[0.05] p-4">{children}</div> : null}
    </section>
  );
}

function PageTypeEditorModal({
  target,
  existingNames,
  onClose,
  onSave,
}: {
  target: PageTypeEditorTarget;
  existingNames: Set<string>;
  onClose: () => void;
  onSave: (value: GbrainPageType) => void;
}) {
  const [value, setValue] = useState<GbrainPageType>(() => clonePageType(target.initial));
  const normalizedName = value.name.trim();
  const duplicate = Boolean(normalizedName && existingNames.has(normalizedName));
  const canSave = Boolean(normalizedName) && !duplicate;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const patch = (next: Partial<GbrainPageType>) => setValue((current) => ({ ...current, ...next }));

  return (
    <div
      className="fixed inset-0 z-[160] flex items-center justify-center bg-slate-950/35 px-4 py-6 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label={target.index === null ? "新增页面类型" : "编辑页面类型"}
        className="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-[24px] bg-white shadow-2xl ring-1 ring-black/[0.08]"
      >
        <header className="flex shrink-0 items-start gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="min-w-0 flex-1">
            <h3 className="text-lg font-semibold text-gray-950">{target.index === null ? "新增页面类型" : "编辑页面类型"}</h3>
            <p className="mt-1 text-xs text-gray-500">只维护页面分类所需的基础字段；其余 gbrain 高级选项使用默认值。</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 hover:bg-black/[0.04] hover:text-gray-700" aria-label="关闭">
            <X className="h-5 w-5" />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="name">
              <input className={inputClass} value={value.name} autoFocus onChange={(event) => patch({ name: event.target.value })} />
              {duplicate ? <span className="mt-1.5 block text-[11px] text-red-500">该名称已存在于自定义 Page Type 列表中。</span> : null}
            </Field>
            <Field label="primitive">
              <select className={inputClass} value={value.primitive} onChange={(event) => patch({ primitive: event.target.value as GbrainPrimitive })}>
                {PRIMITIVES.map((primitive) => <option key={primitive}>{primitive}</option>)}
              </select>
            </Field>
            <Field label="path_prefixes"><StringListEditor value={value.path_prefixes} onChange={(path_prefixes) => patch({ path_prefixes })} /></Field>
            <Field label="aliases"><StringListEditor value={value.aliases} onChange={(aliases) => patch({ aliases })} /></Field>
          </div>
        </div>

        <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-black/[0.06] px-6 py-4">
          <button type="button" onClick={onClose} className="h-10 rounded-xl border border-black/[0.08] px-4 text-sm font-medium text-gray-700 hover:bg-black/[0.025]">取消</button>
          <button
            type="button"
            disabled={!canSave}
            onClick={() => onSave({ ...value, name: normalizedName })}
            className="inline-flex h-10 items-center gap-1.5 rounded-xl bg-[#002fa7] px-4 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-35"
          >
            <Save className="h-4 w-4" /> 保存明细
          </button>
        </footer>
      </section>
    </div>
  );
}

function LinkTypeEditorModal({
  target,
  existingNames,
  onClose,
  onSave,
}: {
  target: LinkTypeEditorTarget;
  existingNames: Set<string>;
  onClose: () => void;
  onSave: (value: GbrainLinkType) => void;
}) {
  const [value, setValue] = useState<GbrainLinkType>(() => cloneLinkType(target.initial));
  const normalizedName = value.name.trim();
  const duplicate = Boolean(normalizedName && existingNames.has(normalizedName));
  const canSave = Boolean(normalizedName) && !duplicate;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[160] flex items-center justify-center bg-slate-950/35 px-4 py-6 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label={target.index === null ? "新增关系类型" : "编辑关系类型"}
        className="flex w-full max-w-2xl flex-col overflow-hidden rounded-[24px] bg-white shadow-2xl ring-1 ring-black/[0.08]"
      >
        <header className="flex items-start gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="min-w-0 flex-1">
            <h3 className="text-lg font-semibold text-gray-950">{target.index === null ? "新增关系类型" : "编辑关系类型"}</h3>
            <p className="mt-1 text-xs text-gray-500">定义关系名称及可选的反向关系；推断规则使用默认值。</p>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 hover:bg-black/[0.04] hover:text-gray-700" aria-label="关闭">
            <X className="h-5 w-5" />
          </button>
        </header>

        <div className="grid gap-4 px-6 py-5 md:grid-cols-2">
          <Field label="name">
            <input className={inputClass} value={value.name} autoFocus onChange={(event) => setValue((current) => ({ ...current, name: event.target.value }))} />
            {duplicate ? <span className="mt-1.5 block text-[11px] text-red-500">该名称已存在于自定义 Link Type 列表中。</span> : null}
          </Field>
          <Field label="inverse" hint="可选">
            <input className={inputClass} value={value.inverse || ""} onChange={(event) => setValue((current) => ({ ...current, inverse: event.target.value || undefined }))} />
          </Field>
        </div>

        <footer className="flex items-center justify-end gap-2 border-t border-black/[0.06] px-6 py-4">
          <button type="button" onClick={onClose} className="h-10 rounded-xl border border-black/[0.08] px-4 text-sm font-medium text-gray-700 hover:bg-black/[0.025]">取消</button>
          <button
            type="button"
            disabled={!canSave}
            onClick={() => onSave({ ...value, name: normalizedName })}
            className="inline-flex h-10 items-center gap-1.5 rounded-xl bg-[#002fa7] px-4 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-35"
          >
            <Save className="h-4 w-4" /> 保存明细
          </button>
        </footer>
      </section>
    </div>
  );
}

type WorkflowResult =
  | { kind: "lint"; value: LlmWikiLintResult }
  | { kind: "compile"; value: LlmWikiCompileResult }
  | null;

function formatBytes(value?: number): string {
  if (!value || value < 1) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function AgentsPreviewModal({
  content,
  path,
  sha256,
  onClose,
}: {
  content: string;
  path: string;
  sha256: string;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<AgentsPreviewMode>("rendered");

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const dialog = (
    <div
      className="fixed inset-0 z-[170] flex items-center justify-center bg-slate-950/35 px-4 py-6 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section role="dialog" aria-modal="true" aria-label="预览 AGENTS.md" className="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-[24px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <header className="flex shrink-0 items-start gap-4 border-b border-black/[0.06] px-6 py-5">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600"><FileCheck2 className="h-5 w-5" /></span>
          <div className="min-w-0 flex-1">
            <h3 className="text-lg font-semibold text-gray-950">AGENTS.md</h3>
            <p className="mt-1 truncate font-mono text-[10px] text-gray-400" title={path}>{path}</p>
            <p className="mt-0.5 font-mono text-[10px] text-gray-400">sha256 · {sha256}</p>
          </div>
          <div className="flex rounded-xl bg-black/[0.035] p-1">
            <button type="button" onClick={() => setMode("rendered")} className={`h-8 rounded-lg px-3 text-xs font-medium ${mode === "rendered" ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500"}`}>阅读</button>
            <button type="button" onClick={() => setMode("source")} className={`h-8 rounded-lg px-3 text-xs font-medium ${mode === "source" ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500"}`}>原文</button>
          </div>
          <button type="button" onClick={onClose} className="rounded-full p-2 text-gray-400 hover:bg-black/[0.04] hover:text-gray-700" aria-label="关闭 AGENTS.md 预览"><X className="h-5 w-5" /></button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50/45 px-6 py-5">
          {mode === "rendered" ? (
            <article className="mx-auto max-w-4xl px-2 py-3 text-sm leading-7 text-gray-700 [&_a]:text-[#002fa7] [&_a]:underline [&_code]:rounded [&_code]:bg-slate-100 [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[0.9em] [&_h1]:mb-5 [&_h1]:text-2xl [&_h1]:font-semibold [&_h1]:text-gray-950 [&_h2]:mb-3 [&_h2]:mt-7 [&_h2]:text-lg [&_h2]:font-semibold [&_h2]:text-gray-950 [&_li]:ml-5 [&_li]:list-disc [&_p]:my-3 [&_pre]:my-4 [&_pre]:overflow-x-auto [&_pre]:rounded-xl [&_pre]:bg-slate-950 [&_pre]:p-4 [&_pre]:text-slate-100 [&_strong]:font-semibold [&_strong]:text-gray-950">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
            </article>
          ) : (
            <pre className="mx-auto max-w-4xl overflow-x-auto whitespace-pre-wrap break-words rounded-2xl bg-slate-950 p-6 font-mono text-xs leading-6 text-slate-100 shadow-sm">{content}</pre>
          )}
        </div>
      </section>
    </div>
  );

  return typeof document === "undefined" ? null : createPortal(dialog, document.body);
}

function WikiWorkflowPanel({
  status,
  selectedRaw,
  busy,
  result,
  onToggleRaw,
  onSelectAll,
  onImportRaw,
  onRebuildAgents,
  onIngest,
  onLint,
  onCompile,
}: {
  status: LlmWikiWorkspaceStatus | null;
  selectedRaw: Set<string>;
  busy: string | null;
  result: WorkflowResult;
  onToggleRaw: (path: string) => void;
  onSelectAll: (paths: string[]) => void;
  onImportRaw: (files: FileList) => void;
  onRebuildAgents: () => void;
  onIngest: () => void;
  onLint: () => void;
  onCompile: (importPages: boolean) => void;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [agentsPreviewOpen, setAgentsPreviewOpen] = useState(false);
  const [importHistoryOpen, setImportHistoryOpen] = useState(false);
  if (!status) {
    return <div className="flex min-h-72 items-center justify-center text-xs text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />加载 Wiki 工作区…</div>;
  }
  const lintResult = result?.kind === "lint" ? result.value : result?.value.lint;
  const working = Boolean(busy);
  const pendingRaw = status.raw.filter((item) => !item.compiled);
  const selectableRaw = pendingRaw.filter((item) => item.integrity === "ok");
  const pendingPaths = selectableRaw.map((item) => item.snapshot_path);
  const selectedPendingCount = pendingPaths.filter((path) => selectedRaw.has(path)).length;
  const compiledCount = status.raw.length - pendingRaw.length;
  const damagedCount = pendingRaw.length - selectableRaw.length;
  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-[#002fa7]/10 bg-gradient-to-br from-[#002fa7]/[0.055] to-white p-4">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold text-gray-950">LLM Wiki 编译工作台</h2>
            <p className="mt-1 text-xs leading-5 text-gray-500">选择不可变 Raw 快照，交给 Agent 按 AGENTS.md 编译并发布 Wiki；如需进入 gbrain，再单独执行预检与 PostgreSQL 入库。</p>
          </div>
          <div className="rounded-xl bg-white/80 px-3 py-2 text-right shadow-sm ring-1 ring-black/[0.04]">
            <p className="text-[10px] text-gray-400">Schema {status.schema_version}</p>
            <p className="font-mono text-[10px] text-gray-600">{status.bundle_hash.slice(0, 12)}</p>
          </div>
        </div>
      </section>

      <section className="rounded-2xl border border-black/[0.06] bg-white p-4">
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600"><FileCheck2 className="h-4 w-4" /></span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-gray-900">1. Agent 操作契约</p>
            <p className="mt-1 truncate font-mono text-[10px] text-gray-400" title={status.agents.path}>AGENTS.md · {status.agents.sha256.slice(0, 12)}</p>
          </div>
          <button type="button" onClick={() => setAgentsPreviewOpen(true)} className="h-8 rounded-xl border border-black/[0.07] px-3 text-[11px] font-medium text-gray-600 hover:text-[#002fa7]">预览 Markdown</button>
          <button type="button" disabled={working} onClick={onRebuildAgents} className="h-8 rounded-xl border border-black/[0.07] px-3 text-[11px] font-medium text-gray-600 hover:text-[#002fa7] disabled:opacity-40">
            {busy === "agents" ? "重建中…" : "按 Schema 重建"}
          </button>
        </div>
      </section>

      <section className="rounded-2xl border border-black/[0.06] bg-white">
        <div className="flex flex-wrap items-center gap-3 border-b border-black/[0.05] px-4 py-3">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-amber-50 text-amber-600"><FileUp className="h-4 w-4" /></span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-gray-900">2. 选择 Raw 并交给 Agent 编译</p>
            <p className="mt-0.5 text-[11px] text-gray-400">待编译 {pendingRaw.length} 个 · 已选择 {selectedPendingCount} 个 · 已编译 {compiledCount} 个{damagedCount ? ` · 完整性异常 ${damagedCount} 个` : ""}</p>
          </div>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".md,.txt,text/markdown,text/plain"
            className="hidden"
            onChange={(event) => {
              if (event.target.files?.length) onImportRaw(event.target.files);
              event.target.value = "";
            }}
          />
          <button type="button" disabled={working} onClick={() => fileInput.current?.click()} className="h-8 rounded-xl border border-black/[0.07] px-3 text-[11px] font-medium text-gray-600 disabled:opacity-40">导入 Raw</button>
          {selectableRaw.length ? <button type="button" onClick={() => onSelectAll(pendingPaths)} className="h-8 rounded-xl border border-black/[0.07] px-3 text-[11px] font-medium text-gray-600">{selectedPendingCount === selectableRaw.length ? "取消全选" : "全选"}</button> : null}
          <button
            type="button"
            disabled={working || selectedPendingCount === 0}
            onClick={onIngest}
            className="inline-flex h-9 items-center gap-1.5 rounded-xl bg-[#002fa7] px-4 text-xs font-medium text-white disabled:opacity-35"
          >
            {busy === "ingest" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Bot className="h-3.5 w-3.5" />}
            {busy === "ingest" ? "正在提交…" : "提交后台编译"}
          </button>
        </div>
        <div className="max-h-64 overflow-y-auto p-3">
          {status.raw.length === 0 ? (
            <div className="rounded-xl border border-dashed border-black/[0.08] px-4 py-8 text-center text-xs text-gray-400">尚无 Raw 快照。先导入 Markdown 或文本资料。</div>
          ) : pendingRaw.length === 0 ? (
            <div className="rounded-xl border border-dashed border-emerald-500/15 bg-emerald-50/50 px-4 py-8 text-center text-xs text-emerald-700">当前 Schema Bundle 下的 Raw 均已成功编译。</div>
          ) : pendingRaw.map((item) => (
            <label key={item.snapshot_path} className="flex cursor-pointer items-center gap-3 rounded-xl px-3 py-2 hover:bg-black/[0.025]">
              <input type="checkbox" disabled={item.integrity !== "ok"} checked={selectedRaw.has(item.snapshot_path)} onChange={() => onToggleRaw(item.snapshot_path)} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-medium text-gray-800">{item.title || item.asset_id || item.snapshot_path}</span>
                <span className="mt-0.5 block truncate font-mono text-[10px] text-gray-400">{item.snapshot_path}</span>
              </span>
              <span className="shrink-0 text-[10px] text-gray-400">{formatBytes(item.size_bytes)}</span>
              <span className={`h-2 w-2 shrink-0 rounded-full ${item.integrity === "ok" ? "bg-emerald-400" : "bg-red-400"}`} title={`完整性：${item.integrity}`} />
            </label>
          ))}
        </div>
      </section>

      <section className="overflow-hidden rounded-2xl border border-black/[0.06] bg-white">
        <div className="flex items-start gap-3 p-4">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-violet-50 text-violet-600"><BookOpen className="h-4 w-4" /></span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-gray-900">3. Wiki 发布结果</p>
            <p className="mt-1 text-[11px] text-gray-400">{status.wiki.length} 个页面 · index {status.files.index ? "就绪" : "缺失"} · log {status.files.log ? "就绪" : "缺失"}</p>
          </div>
          <button
            type="button"
            disabled={working}
            onClick={onLint}
            title="检查页面格式、Schema 类型、Raw 来源、Wiki 链接、目录和索引是否完整"
            className="h-8 rounded-xl border border-black/[0.07] px-3 text-[11px] font-medium text-gray-600 hover:text-[#002fa7] disabled:opacity-40"
          >
            {busy === "lint" ? "检查中…" : "检查完整性"}
          </button>
        </div>
        {result?.kind === "lint" ? (
          <div className={`border-t px-4 py-3 text-xs ${result.value.ok ? "border-emerald-500/10 bg-emerald-50/70 text-emerald-800" : "border-red-500/10 bg-red-50/70 text-red-700"}`}>
            <p className="font-semibold">{result.value.ok ? "检查通过" : "发现需要处理的问题"}</p>
            <p className="mt-1">{result.value.counts.pages} 个页面，{result.value.counts.errors} 个问题，{result.value.counts.warnings} 个提醒。</p>
            {result.value.errors.slice(0, 5).map((item) => <p key={`${item.code}-${item.path}`} className="mt-1 font-mono text-[10px]">{item.path}: {item.message}</p>)}
          </div>
        ) : null}
      </section>

      <section className="rounded-2xl border border-black/[0.06] bg-white p-4">
        <div className="flex flex-wrap items-center gap-3">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-50 text-sky-600"><ShieldCheck className="h-4 w-4" /></span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-gray-900">4. gbrain 前置检查与入库</p>
            <p className="mt-1 text-[11px] text-gray-400">CLI {status.gbrain.cli_installed ? "已安装" : "未安装"} · PostgreSQL {status.gbrain.postgres_configured ? "已配置" : "未配置"}</p>
          </div>
          <Link href="/settings?category=knowledge#gbrain-database" className="h-9 rounded-xl border border-black/[0.07] px-3 py-2 text-xs font-medium text-gray-600 hover:text-[#002fa7]">知识库设置</Link>
          <button type="button" disabled={working || !status.gbrain.cli_installed} onClick={() => onCompile(false)} className="h-9 rounded-xl border border-[#002fa7]/15 px-3 text-xs font-medium text-[#002fa7] disabled:opacity-35">{busy === "compile" ? "预检中…" : "Schema + Wiki 预检"}</button>
          <button type="button" disabled={working || !status.gbrain.cli_installed || !status.gbrain.models.configured || !status.gbrain.postgres_configured || status.wiki.length === 0} onClick={() => onCompile(true)} className="h-9 rounded-xl bg-[#002fa7] px-3 text-xs font-medium text-white disabled:opacity-35">{busy === "import" ? "导入中…" : "导入 PostgreSQL"}</button>
        </div>
        {status.gbrain.models.configured ? (
          <div className="mt-3 grid gap-2 text-[10px] sm:grid-cols-2">
            <div className="rounded-xl bg-teal-50 px-3 py-2 text-teal-800">
              <span className="text-teal-600">Embedding</span> · {status.gbrain.models.embedding?.provider}:{status.gbrain.models.embedding?.name} · {status.gbrain.models.embedding?.dimension} 维
            </div>
            <div className="rounded-xl bg-violet-50 px-3 py-2 text-violet-800">
              <span className="text-violet-600">Think</span> · {status.gbrain.models.think?.provider}:{status.gbrain.models.think?.name}
            </div>
          </div>
        ) : (
          <p className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-[10px] text-amber-700">模型未就绪：{status.gbrain.models.error || "请先在知识库设置中选择模型"}</p>
        )}
        {!status.gbrain.postgres_configured ? (
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-amber-100 bg-amber-50/60 px-3 py-2.5">
            <p className="text-[10px] leading-4 text-amber-700">GBrain PostgreSQL 尚未配置，请先在知识库设置中连接独立数据库。</p>
            <Link href="/settings?category=knowledge#gbrain-database" className="shrink-0 rounded-lg bg-white px-2.5 py-1.5 text-[10px] font-medium text-amber-800 ring-1 ring-amber-200 hover:bg-amber-50">
              前往配置
            </Link>
          </div>
        ) : (
          <p className="mt-3 rounded-xl bg-emerald-50 px-3 py-2 text-[10px] text-emerald-700">GBrain PostgreSQL 与 runtime 已就绪。</p>
        )}
        {status.gbrain.imports.records.length > 0 ? (
          <div className="mt-3 overflow-hidden rounded-xl border border-black/[0.06] bg-black/[0.015]">
            <button
              type="button"
              onClick={() => setImportHistoryOpen((current) => !current)}
              className="flex w-full items-center gap-3 px-3 py-2.5 text-left"
              aria-expanded={importHistoryOpen}
            >
              {importHistoryOpen ? <ChevronDown className="h-3.5 w-3.5 text-gray-400" /> : <ChevronRight className="h-3.5 w-3.5 text-gray-400" />}
              <span className="min-w-0 flex-1">
                <span className="block text-xs font-semibold text-gray-800">PostgreSQL 导入记录</span>
                <span className="mt-0.5 block text-[10px] text-gray-400">
                  {status.gbrain.imports.counts.imports} 次导入 · {status.gbrain.imports.counts.pages} 个页面 · {status.gbrain.imports.counts.links} 条关系 · {status.gbrain.imports.counts.chunks} 个 chunks
                </span>
              </span>
              <span className="shrink-0 text-[10px] text-gray-400">最近 {status.gbrain.imports.records.length} 条</span>
            </button>
            {importHistoryOpen ? (
              <div className="max-h-64 space-y-2 overflow-y-auto border-t border-black/[0.05] p-3">
                {status.gbrain.imports.records.map((record) => (
                  <div key={record.id} className="rounded-xl bg-white px-3 py-2.5 ring-1 ring-black/[0.04]">
                    <div className="flex flex-wrap items-start gap-2">
                      <p className="min-w-0 flex-1 text-xs font-medium text-gray-800">{record.summary || `导入 ${record.pages_updated.length} 个页面`}</p>
                      <time className="shrink-0 text-[10px] text-gray-400" dateTime={record.created_at}>{formatDateTime(record.created_at)}</time>
                    </div>
                    <p className="mt-1 text-[10px] text-gray-400">{record.source_type || "unknown"} · {record.pages_updated.length} 个页面</p>
                    {record.pages_updated.length > 0 ? (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {record.pages_updated.map((slug) => <span key={slug} className="rounded-md bg-[#002fa7]/[0.055] px-2 py-1 font-mono text-[9px] text-[#002fa7]">{slug}</span>)}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        {result?.kind === "compile" ? (
          <div className={`mt-3 rounded-xl px-3 py-2 text-xs ${result.value.ok ? "bg-emerald-50 text-emerald-800" : "bg-red-50 text-red-700"}`}>
            <p className="font-semibold">{result.value.ok ? "gbrain 运行通过" : "gbrain 运行未通过"} · {result.value.phase}</p>
            {lintResult ? <p className="mt-1">{lintResult.counts.pages} 个页面，{lintResult.counts.errors} 个错误，{lintResult.counts.warnings} 个警告。</p> : null}
            {lintResult?.errors.slice(0, 5).map((item) => <p key={`${item.code}-${item.path}`} className="mt-1 font-mono text-[10px]">{item.path}: {item.message}</p>)}
          </div>
        ) : null}
      </section>
      {agentsPreviewOpen ? (
        <AgentsPreviewModal
          content={status.agents.content}
          path={status.agents.path}
          sha256={status.agents.sha256}
          onClose={() => setAgentsPreviewOpen(false)}
        />
      ) : null}
    </div>
  );
}

function BuiltinCard({
  pack,
  expanded,
  currentParent,
  onToggle,
  onUse,
}: {
  pack: GbrainSchemaCatalogPack;
  expanded: boolean;
  currentParent: boolean;
  onToggle: () => void;
  onUse: () => void;
}) {
  return (
    <article
      className={`overflow-hidden rounded-2xl border transition ${expanded ? "xl:col-span-2" : ""} ${
        expanded ? "border-[#002fa7]/30 bg-[#002fa7]/[0.025]" : "border-black/[0.06] bg-white hover:border-black/[0.12]"
      }`}
    >
      <button
        type="button"
        onClick={onToggle}
        className="block w-full p-3 text-left"
        aria-expanded={expanded}
      >
        <div className="flex items-center gap-2">
          <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-gray-900">{pack.name}</h3>
          <span className="shrink-0 font-mono text-[10px] text-gray-400">v{pack.version}</span>
          {expanded ? <ChevronDown className="h-4 w-4 shrink-0 text-gray-400" /> : <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" />}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-gray-500">
          <span className="rounded-lg bg-black/[0.035] px-2 py-1">{pack.page_type_count} page types</span>
          <span className="rounded-lg bg-black/[0.035] px-2 py-1">{pack.link_type_count} link types</span>
          {pack.recommended ? (
            <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">推荐</span>
          ) : null}
          {pack.legacy ? (
            <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">旧基线</span>
          ) : null}
          {currentParent ? (
            <span className="rounded-full bg-[#002fa7]/[0.07] px-2 py-0.5 text-[10px] font-medium text-[#002fa7]">当前父包</span>
          ) : null}
        </div>
      </button>
      {expanded ? (
        <div className="border-t border-black/[0.055] p-3">
          <p className="text-xs leading-5 text-gray-500">{pack.description || "无描述"}</p>
          {pack.extends ? (
            <p className="mt-2 font-mono text-[10px] text-gray-400">extends {pack.extends}</p>
          ) : null}
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <div>
              <div className="flex items-center justify-between border-b border-black/[0.055] pb-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                <span>Page Types</span><span>Primitive</span>
              </div>
              <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
                {pack.manifest.page_types.length ? pack.manifest.page_types.map((item) => (
                  <div key={item.name} className="flex min-w-0 items-center gap-3 rounded-lg bg-[#002fa7]/[0.045] px-2.5 py-1.5 text-[10px]">
                    <span className="min-w-0 flex-1 truncate font-mono font-medium text-[#002fa7]">{item.name}</span>
                    <span className="shrink-0 font-mono text-gray-400">{item.primitive}</span>
                  </div>
                )) : <span className="text-[10px] text-gray-400">此 pack 未新增页面类型</span>}
              </div>
            </div>
            <div>
              <div className="flex items-center justify-between border-b border-black/[0.055] pb-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                <span>Link Types</span><span>Inverse</span>
              </div>
              <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
                {pack.manifest.link_types.length ? pack.manifest.link_types.map((item) => (
                  <div key={item.name} className="flex min-w-0 items-center gap-3 rounded-lg bg-violet-500/[0.045] px-2.5 py-1.5 text-[10px]">
                    <span className="min-w-0 flex-1 truncate font-mono font-medium text-violet-700">{item.name}</span>
                    <span className="shrink-0 font-mono text-gray-400">{item.inverse || "—"}</span>
                  </div>
                )) : <span className="text-[10px] text-gray-400">此 pack 未新增关系类型</span>}
              </div>
            </div>
          </div>
          {!currentParent ? (
            <button
              type="button"
              onClick={onUse}
              className="mt-3 inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-xl border border-black/[0.07] bg-white text-xs font-medium text-gray-700 transition hover:border-[#002fa7]/20 hover:text-[#002fa7]"
            >
              <Layers3 className="h-3.5 w-3.5" />
              设为父包
            </button>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

export default function BrainSchemaPage() {
  const router = useRouter();
  const {
    sidebarOpen,
    toggleSidebar,
    sidebarWidth,
    setSidebarWidth,
  } = useApp();
  const [mounted, setMounted] = useState(false);
  const [catalog, setCatalog] = useState<GbrainSchemaCatalog | null>(null);
  const [bundle, setBundle] = useState<BrainSchemaBundle | null>(null);
  const [draft, setDraft] = useState<GbrainSchemaPackManifest | null>(null);
  const [preview, setPreview] = useState<BrainSchemaPreview | null>(null);
  const [selectedPack, setSelectedPack] = useState<string>("gbrain-base-v2");
  const [expandedPack, setExpandedPack] = useState<string | null>(null);
  const [metadataOpen, setMetadataOpen] = useState(false);
  const [rawPreviewOpen, setRawPreviewOpen] = useState(false);
  const [pageTypeEditor, setPageTypeEditor] = useState<PageTypeEditorTarget | null>(null);
  const [linkTypeEditor, setLinkTypeEditor] = useState<LinkTypeEditorTarget | null>(null);
  const [tab, setTab] = useState<MainTab>("workflow");
  const [rawTarget, setRawTarget] = useState<RawTarget>("parent");
  const [workspace, setWorkspace] = useState<LlmWikiWorkspaceStatus | null>(null);
  const [selectedRaw, setSelectedRaw] = useState<Set<string>>(new Set());
  const [workflowBusy, setWorkflowBusy] = useState<string | null>(null);
  const [workflowResult, setWorkflowResult] = useState<WorkflowResult>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [initializing, setInitializing] = useState(false);
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => {
      setNotice((current) => (current === notice ? null : current));
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (workflowResult?.kind !== "compile" || !workflowResult.value.ok) return;
    const currentResult = workflowResult;
    const timer = window.setTimeout(() => {
      setWorkflowResult((current) => (current === currentResult ? null : current));
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [workflowResult]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextCatalog, nextBundle] = await Promise.all([getBrainSchemaCatalog(), getBrainSchemaBundle()]);
      setPageTypeEditor(null);
      setLinkTypeEditor(null);
      setCatalog(nextCatalog);
      setBundle(nextBundle);
      if (nextBundle) {
        const nextWorkspace = await getLlmWikiWorkspaceStatus();
        setWorkspace(nextWorkspace);
        setSelectedRaw((current) => new Set(Array.from(current).filter((path) => (
          nextWorkspace.raw.some((item) => item.snapshot_path === path && !item.compiled && item.integrity === "ok")
        ))));
        setDraft(cloneManifest(nextBundle.custom.manifest));
        setSelectedPack(nextBundle.custom.manifest.extends || nextCatalog.packs[0]?.name || "");
      } else {
        setWorkspace(null);
        setDraft(null);
        setPreview(null);
        setSelectedPack(nextCatalog.packs.find((pack) => pack.recommended)?.name || nextCatalog.packs[0]?.name || "");
      }
    } catch (loadError) {
      setError(messageOf(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!draft) return;
    let cancelled = false;
    setValidating(true);
    setValidationError(null);
    const timer = window.setTimeout(() => {
      previewBrainCustomSchema(draft)
        .then((nextPreview) => {
          if (!cancelled) setPreview(nextPreview);
        })
        .catch((previewError) => {
          if (!cancelled) {
            setPreview(null);
            setValidationError(messageOf(previewError));
          }
        })
        .finally(() => {
          if (!cancelled) setValidating(false);
        });
    }, 500);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [draft]);

  const initialize = useCallback(async () => {
    setInitializing(true);
    setError(null);
    try {
      const next = await initializeBrainSchema();
      setBundle(next);
      setDraft(cloneManifest(next.custom.manifest));
      setSelectedPack(next.custom.manifest.extends || "gbrain-base-v2");
      setWorkspace(await getLlmWikiWorkspaceStatus());
      setTab("workflow");
      setRawTarget("custom");
      setNotice("已创建 raw / wiki / index / log / AGENTS.md 与 Schema Bundle。知识正文不会被覆盖，生成式 AGENTS.md 会按活动 Schema 重建。");
    } catch (initializeError) {
      setError(messageOf(initializeError));
    } finally {
      setInitializing(false);
    }
  }, []);

  const save = useCallback(async () => {
    if (!draft || !bundle || !preview || validationError) return;
    setSaving(true);
    setError(null);
    try {
      const next = await saveBrainCustomSchema(
        draft,
        bundle.custom.manifest_sha256,
        bundle.bundle_hash,
      );
      setBundle(next);
      setDraft(cloneManifest(next.custom.manifest));
      setWorkspace(await getLlmWikiWorkspaceStatus());
      setNotice(`Schema Bundle 已保存：${next.bundle_hash.slice(0, 12)}`);
    } catch (saveError) {
      setError(messageOf(saveError));
    } finally {
      setSaving(false);
    }
  }, [bundle, draft, preview, validationError]);

  const selectedBuiltin = useMemo(
    () => catalog?.packs.find((pack) => pack.name === selectedPack) || catalog?.packs[0] || null,
    [catalog, selectedPack],
  );

  const parentPageTypeNames = useMemo(() => {
    const names = new Set<string>();
    const manifests = new Map((catalog?.packs || []).map((pack) => [pack.name, pack.manifest]));
    let parentName = draft?.extends || null;
    const visited = new Set<string>();
    while (parentName && !visited.has(parentName)) {
      visited.add(parentName);
      const manifest = manifests.get(parentName);
      if (!manifest) break;
      manifest.page_types.forEach((item) => names.add(item.name));
      parentName = manifest.extends || null;
    }
    return names;
  }, [catalog, draft?.extends]);

  const parentLinkTypeNames = useMemo(() => {
    const names = new Set<string>();
    const manifests = new Map((catalog?.packs || []).map((pack) => [pack.name, pack.manifest]));
    let parentName = draft?.extends || null;
    const visited = new Set<string>();
    while (parentName && !visited.has(parentName)) {
      visited.add(parentName);
      const manifest = manifests.get(parentName);
      if (!manifest) break;
      manifest.link_types.forEach((item) => names.add(item.name));
      parentName = manifest.extends || null;
    }
    return names;
  }, [catalog, draft?.extends]);

  const rawText = useMemo(() => {
    if (rawTarget === "parent") return selectedBuiltin?.raw_yaml || "# 请选择一个官方 Schema Pack";
    if (rawTarget === "brain") return bundle?.brain_schema.raw_yaml || "# 尚未初始化 brain.schema.yaml";
    if (rawTarget === "resolved") return preview?.resolved.raw_yaml || bundle?.resolved.raw_yaml || "# 等待 Schema 校验";
    if (preview?.custom.raw_yaml) return preview.custom.raw_yaml;
    if (draft && validationError) {
      return `# 当前结构化草稿尚未通过官方校验\n# ${validationError.replaceAll("\n", " ")}\n${JSON.stringify(draft, null, 2)}\n`;
    }
    return bundle?.custom.raw_yaml || "# 尚未初始化自定义 pack.yaml";
  }, [bundle, draft, preview, rawTarget, selectedBuiltin, validationError]);

  const dirty = useMemo(() => {
    if (!bundle || !draft) return false;
    return JSON.stringify(draft) !== JSON.stringify(bundle.custom.manifest);
  }, [bundle, draft]);

  const versionNeedsBump = Boolean(
    dirty && bundle && draft && draft.version === bundle.custom.manifest.version,
  );

  const chooseParent = useCallback((name: string | null) => {
    setSelectedPack(name || "");
    setDraft((current) => (current ? { ...current, extends: name } : current));
    setTab("custom");
    setRawTarget("custom");
  }, []);

  const savePageTypeDetail = useCallback((value: GbrainPageType) => {
    const targetIndex = pageTypeEditor?.index ?? null;
    setDraft((current) => {
      if (!current) return current;
      const pageTypes = [...current.page_types];
      if (targetIndex === null) pageTypes.push(value);
      else pageTypes[targetIndex] = value;
      return { ...current, page_types: pageTypes };
    });
    setPageTypeEditor(null);
  }, [pageTypeEditor]);

  const saveLinkTypeDetail = useCallback((value: GbrainLinkType) => {
    const targetIndex = linkTypeEditor?.index ?? null;
    setDraft((current) => {
      if (!current) return current;
      const linkTypes = [...current.link_types];
      if (targetIndex === null) linkTypes.push(value);
      else linkTypes[targetIndex] = value;
      return { ...current, link_types: linkTypes };
    });
    setLinkTypeEditor(null);
  }, [linkTypeEditor]);

  const rebuildAgents = useCallback(async () => {
    setWorkflowBusy("agents");
    setError(null);
    try {
      const next = await rebuildLlmWikiAgents();
      setBundle(next);
      setWorkspace(await getLlmWikiWorkspaceStatus());
      setNotice("AGENTS.md 已按当前活动 Schema 重建。");
    } catch (actionError) {
      setError(messageOf(actionError));
    } finally {
      setWorkflowBusy(null);
    }
  }, []);

  const recoverAgentsMismatch = useCallback(async () => {
    setWorkflowBusy("agents");
    setError(null);
    try {
      const [next, nextCatalog] = await Promise.all([
        rebuildLlmWikiAgents(),
        getBrainSchemaCatalog(),
      ]);
      const nextWorkspace = await getLlmWikiWorkspaceStatus();
      setCatalog(nextCatalog);
      setBundle(next);
      setDraft(cloneManifest(next.custom.manifest));
      setSelectedPack(next.custom.manifest.extends || nextCatalog.packs[0]?.name || "");
      setWorkspace(nextWorkspace);
      setTab("workflow");
      setNotice("AGENTS.md 已按当前活动 Schema 重建。");
    } catch (actionError) {
      setError(messageOf(actionError));
    } finally {
      setWorkflowBusy(null);
    }
  }, []);

  const importRaw = useCallback(async (files: FileList) => {
    setWorkflowBusy("raw");
    setError(null);
    try {
      const imported: string[] = [];
      for (const file of Array.from(files)) {
        const result = await snapshotLlmWikiRaw({
          source_id: "manual-upload",
          asset_id: `${file.name}-${file.lastModified}`,
          title: file.name.replace(/\.(md|txt)$/i, ""),
          content: await file.text(),
          source_path: file.name,
        });
        if (typeof result.snapshot_path === "string") imported.push(result.snapshot_path);
      }
      const nextWorkspace = await getLlmWikiWorkspaceStatus();
      setWorkspace(nextWorkspace);
      setSelectedRaw((current) => new Set([...Array.from(current), ...imported]));
      setNotice(`已导入 ${imported.length} 个不可变 Raw 快照。`);
    } catch (actionError) {
      setError(messageOf(actionError));
    } finally {
      setWorkflowBusy(null);
    }
  }, []);

  const runWikiLint = useCallback(async () => {
    setWorkflowBusy("lint");
    setError(null);
    try {
      const value = await lintLlmWiki();
      setWorkflowResult({ kind: "lint", value });
      setWorkspace(await getLlmWikiWorkspaceStatus());
    } catch (actionError) {
      setError(messageOf(actionError));
    } finally {
      setWorkflowBusy(null);
    }
  }, []);

  const runGbrainCompile = useCallback(async (importPages: boolean) => {
    setWorkflowBusy(importPages ? "import" : "compile");
    setError(null);
    try {
      const value = await compileLlmWikiGbrain(importPages);
      setWorkflowResult({ kind: "compile", value });
      setWorkspace(await getLlmWikiWorkspaceStatus());
    } catch (actionError) {
      setError(messageOf(actionError));
    } finally {
      setWorkflowBusy(null);
    }
  }, []);

  const startAgentIngest = useCallback(async () => {
    const rawPaths = Array.from(selectedRaw)
      .filter((path) => workspace?.raw.some((item) => (
        item.snapshot_path === path && !item.compiled && item.integrity === "ok"
      )))
      .sort();
    if (!rawPaths.length) return;
    if (dirty) {
      setError("存在未保存的 Schema 草稿。请先升级版本并保存 Schema，再开始编译 Raw。");
      return;
    }
    setWorkflowBusy("ingest");
    setError(null);
    try {
      const job = await createLlmWikiIngestJob(rawPaths);
      setSelectedRaw(new Set());
      setNotice(`后台编译任务已提交：${job.title || job.id}`);
      router.push("/knowledge/imports?filter=wiki");
    } catch (actionError) {
      setError(messageOf(actionError));
      setWorkflowBusy(null);
    }
  }, [dirty, router, selectedRaw, workspace]);

  const agentsMismatch = Boolean(
    error?.includes("LLM Wiki AGENTS.md does not match the active Schema Bundle"),
  );

  return (
    <div className="h-screen app-bg text-gray-900">
      <div className="fixed left-3 top-3 z-[80]">
        <Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact />
      </div>
      <div className="flex h-full overflow-hidden">
        <div
          className="workspace-sidebar-shell shrink-0 overflow-hidden panel-transition"
          style={{ width: sidebarOpen ? sidebarWidth : 0 }}
        >
          <div style={{ width: sidebarWidth, minWidth: 200 }} className="flex h-full flex-col">
            <div className="h-11 shrink-0" />
            <div className="min-h-0 flex-1 overflow-hidden"><Sidebar /></div>
          </div>
        </div>
        {mounted && sidebarOpen ? (
          <ResizeHandle direction="left" onResize={(delta) => setSidebarWidth((value) => Math.max(200, value + delta))} />
        ) : null}

        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <header className="flex min-h-[72px] shrink-0 items-center gap-4 border-b border-black/[0.055] bg-white/80 px-5 backdrop-blur-xl">
            <Link href="/knowledge" className="rounded-xl p-2 text-gray-400 hover:bg-black/[0.04] hover:text-gray-700" title="返回知识库">
              <ArrowLeft className="h-4 w-4" />
            </Link>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <DatabaseZap className="h-5 w-5 text-[#002fa7]" />
                <h1 className="truncate text-lg font-semibold text-gray-950">LLM WiKi Studio</h1>
              </div>
              <p className="mt-0.5 truncate text-xs text-gray-500">统一管理 Wiki 编译、知识 Schema 与 Agent 操作契约</p>
            </div>
            {bundle ? (
              <div className="hidden text-right lg:block">
                <p className="font-mono text-[10px] text-gray-400">BUNDLE</p>
                <p className="font-mono text-[11px] text-gray-600">{bundle.bundle_hash.slice(0, 12)}</p>
              </div>
            ) : null}
            <button
              type="button"
              onClick={() => {
                if (!dirty || window.confirm("刷新会丢弃未保存的 Schema 草稿，继续吗？")) void load();
              }}
              disabled={loading}
              className="inline-flex h-9 items-center gap-1.5 rounded-full border border-black/[0.07] bg-white px-3 text-xs font-medium text-gray-700 disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} /> 刷新
            </button>
            <button
              type="button"
              onClick={() => void save()}
              disabled={!dirty || versionNeedsBump || saving || validating || Boolean(validationError) || !preview}
              title={versionNeedsBump ? `Schema 内容变化必须将版本升级到高于 ${bundle?.custom.manifest.version}` : undefined}
              className="inline-flex h-9 items-center gap-1.5 rounded-full bg-[#002fa7] px-4 text-xs font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-35"
            >
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              保存 Schema
            </button>
          </header>

          {loading ? (
            <div className="flex flex-1 items-center justify-center text-sm text-gray-400">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" /> 正在读取 gbrain 官方 Schema…
            </div>
          ) : agentsMismatch ? (
            <div className="flex flex-1 items-center justify-center p-6">
              <div className="w-full max-w-xl rounded-[28px] border border-amber-500/20 bg-white/95 p-8 text-center shadow-sm">
                <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-50 text-amber-600">
                  <FileCheck2 className="h-6 w-6" />
                </div>
                <h2 className="mt-5 text-xl font-semibold text-gray-950">Agent 操作契约需要更新</h2>
                <p className="mt-2 text-sm leading-6 text-gray-500">
                  当前 Schema 本身没有丢失，只是生成式 AGENTS.md 与活动 Schema 不一致。重建操作不会修改 Raw 或已有 Wiki 页面。
                </p>
                <button
                  type="button"
                  onClick={() => void recoverAgentsMismatch()}
                  disabled={workflowBusy === "agents"}
                  className="mt-5 inline-flex h-10 items-center gap-2 rounded-full bg-[#002fa7] px-5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {workflowBusy === "agents" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                  {workflowBusy === "agents" ? "正在重建…" : "按当前 Schema 重建 AGENTS.md"}
                </button>
                {error ? <p className="mt-4 font-mono text-[10px] text-amber-700/70">{error}</p> : null}
              </div>
            </div>
          ) : error && !catalog ? (
            <div className="m-6 rounded-2xl border border-red-500/15 bg-red-50 p-4 text-sm text-red-700">{error}</div>
          ) : !bundle ? (
            <div className="flex flex-1 items-center justify-center p-6">
              <div className="max-w-lg rounded-[28px] border border-black/[0.06] bg-white/90 p-8 text-center shadow-sm">
                <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-[#002fa7]/10 text-[#002fa7]">
                  <DatabaseZap className="h-6 w-6" />
                </div>
                <h2 className="mt-5 text-xl font-semibold text-gray-950">初始化 LLM WiKi Studio 工作区</h2>
                <p className="mt-2 text-sm leading-6 text-gray-500">
                  创建 raw、wiki、index、log、AGENTS.md 和 Schema Bundle。初始化是幂等的，不覆盖知识正文；生成式 AGENTS.md 会按活动 Schema 重建。
                </p>
                <p className="mt-3 rounded-xl bg-black/[0.03] px-3 py-2 font-mono text-[11px] text-gray-500">
                  默认父包：{catalog?.packs.find((pack) => pack.recommended)?.name || "gbrain-base-v2"}
                </p>
                <button
                  type="button"
                  onClick={() => void initialize()}
                  disabled={initializing}
                  className="mt-5 inline-flex h-10 items-center gap-2 rounded-full bg-[#002fa7] px-5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {initializing ? <Loader2 className="h-4 w-4 animate-spin" /> : <DatabaseZap className="h-4 w-4" />}
                  开始初始化
                </button>
              </div>
            </div>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col">
              {(error || notice || validationError) ? (
                <div className="shrink-0 px-5 pt-3">
                  <div
                    className={`flex items-start gap-2 rounded-xl border px-3 py-2 text-xs ${
                      error || validationError
                        ? "border-red-500/15 bg-red-50 text-red-700"
                        : "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                    }`}
                  >
                    {error || validationError ? <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
                    <span className="break-all">{error || validationError || notice}</span>
                  </div>
                </div>
              ) : null}

              <div className="flex min-h-0 flex-1 gap-4 p-5">
                <section className="flex min-w-0 flex-[1.3] flex-col overflow-hidden rounded-[24px] border border-black/[0.06] bg-white/65">
                  <nav className="flex shrink-0 items-center gap-1 border-b border-black/[0.055] bg-white/85 p-2">
                    {([
                      ["workflow", "编译工作台", Bot],
                      ["builtins", "官方内置", Layers3],
                      ["custom", "我的扩展", FileCode2],
                      ["resolved", "合并结果", DatabaseZap],
                    ] as const).map(([value, label, Icon]) => (
                      <button
                        key={value}
                        type="button"
                        onClick={() => setTab(value)}
                        className={`inline-flex h-9 items-center gap-1.5 rounded-xl px-3 text-xs font-medium ${
                          tab === value ? "bg-[#002fa7] text-white" : "text-gray-500 hover:bg-black/[0.04] hover:text-gray-800"
                        }`}
                      >
                        <Icon className="h-3.5 w-3.5" /> {label}
                      </button>
                    ))}
                    <div className="ml-auto flex items-center gap-1.5 pr-2 text-[11px]">
                      {tab !== "workflow" && validating ? (
                        <span className="inline-flex items-center text-gray-400"><Loader2 className="mr-1 h-3 w-3 animate-spin" />校验中</span>
                      ) : tab !== "workflow" && preview ? (
                        <span className="inline-flex items-center text-emerald-600"><Check className="mr-1 h-3 w-3" />{preview?.validation_mode === "structural" ? "结构有效 · 保存时运行 gbrain" : "官方格式有效"}</span>
                      ) : null}
                      {dirty ? <span className="h-2 w-2 rounded-full bg-amber-400" title="有未保存更改" /> : null}
                      {versionNeedsBump ? <span className="text-amber-600">请升级 SemVer</span> : null}
                    </div>
                    {tab !== "workflow" && !rawPreviewOpen ? (
                      <button
                        type="button"
                        onClick={() => setRawPreviewOpen(true)}
                        className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-black/[0.07] bg-white px-2.5 text-[11px] font-medium text-gray-600 hover:border-[#002fa7]/20 hover:text-[#002fa7]"
                        title="打开原始文件预览"
                      >
                        <PanelRightOpen className="h-3.5 w-3.5" /> 原始文件
                      </button>
                    ) : null}
                  </nav>

                  <div className="min-h-0 flex-1 overflow-y-auto p-4">
                    {tab === "workflow" ? (
                      <WikiWorkflowPanel
                        status={workspace}
                        selectedRaw={selectedRaw}
                        busy={workflowBusy}
                        result={workflowResult}
                        onToggleRaw={(path) => setSelectedRaw((current) => {
                          const next = new Set(current);
                          if (next.has(path)) next.delete(path);
                          else next.add(path);
                          return next;
                        })}
                        onSelectAll={(paths) => setSelectedRaw((current) => {
                          const allSelected = paths.every((path) => current.has(path));
                          if (allSelected) return new Set(Array.from(current).filter((path) => !paths.includes(path)));
                          return new Set([...Array.from(current), ...paths]);
                        })}
                        onImportRaw={(files) => void importRaw(files)}
                        onRebuildAgents={() => void rebuildAgents()}
                        onIngest={() => void startAgentIngest()}
                        onLint={() => void runWikiLint()}
                        onCompile={(importPages) => void runGbrainCompile(importPages)}
                      />
                    ) : null}

                    {tab === "builtins" ? (
                      <div>
                        <div className="mb-4">
                          <h2 className="text-sm font-semibold text-gray-900">gbrain 内置 Schema Packs</h2>
                          <p className="mt-1 text-xs leading-5 text-gray-500">
                            内容直接来自当前安装的 gbrain。点击 pack 展开详情，字段保留官方原始 identifier。
                          </p>
                        </div>
                        <div className="grid items-start gap-3 xl:grid-cols-2">
                          {catalog?.packs.map((pack) => (
                            <BuiltinCard
                              key={pack.name}
                              pack={pack}
                              expanded={expandedPack === pack.name}
                              currentParent={draft?.extends === pack.name}
                              onToggle={() => {
                                setSelectedPack(pack.name);
                                setRawTarget("parent");
                                setExpandedPack((current) => current === pack.name ? null : pack.name);
                              }}
                              onUse={() => chooseParent(pack.name)}
                            />
                          ))}
                        </div>
                      </div>
                    ) : null}

                    {tab === "custom" && draft ? (
                      <div className="space-y-4">
                        <section className="rounded-2xl border border-black/[0.06] bg-white">
                          <button
                            type="button"
                            onClick={() => setMetadataOpen((value) => !value)}
                            className="flex w-full items-center gap-3 px-4 py-3 text-left"
                            aria-expanded={metadataOpen}
                          >
                            {metadataOpen ? <ChevronDown className="h-4 w-4 shrink-0 text-gray-400" /> : <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" />}
                            <div className="min-w-0 flex-1">
                              <h2 className="text-sm font-semibold text-gray-900">官方 Manifest 元数据</h2>
                              <p className="mt-0.5 text-[11px] text-gray-400">字段名和数据形状与 gbrain-schema-pack-v1 保持一致。</p>
                            </div>
                          </button>
                          {metadataOpen ? <div className="grid gap-3 border-t border-black/[0.05] p-4 md:grid-cols-2">
                            <Field label="api_version"><input className={inputClass} value={draft.api_version} disabled /></Field>
                            <Field label="name" hint="P0 固定"><input className={inputClass} value={draft.name} disabled /></Field>
                            <Field label="version" hint="SemVer">
                              <input className={inputClass} value={draft.version} onChange={(event) => setDraft({ ...draft, version: event.target.value })} />
                            </Field>
                            <Field label="extends" hint="官方父包">
                              <select
                                className={inputClass}
                                value={draft.extends ?? "__none__"}
                                onChange={(event) => chooseParent(event.target.value === "__none__" ? null : event.target.value)}
                              >
                                <option value="__none__">不继承（extends: null）</option>
                                {catalog?.packs.map((pack) => <option key={pack.name} value={pack.name}>{pack.name}</option>)}
                              </select>
                            </Field>
                            <div className="md:col-span-2">
                              <Field label="description">
                                <input className={inputClass} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
                              </Field>
                            </div>
                            <Field label="author"><input className={inputClass} value={draft.author || ""} onChange={(event) => setDraft({ ...draft, author: event.target.value || undefined })} /></Field>
                            <Field label="license"><input className={inputClass} value={draft.license || ""} onChange={(event) => setDraft({ ...draft, license: event.target.value || undefined })} /></Field>
                            <Field label="homepage"><input className={inputClass} value={draft.homepage || ""} onChange={(event) => setDraft({ ...draft, homepage: event.target.value || undefined })} /></Field>
                            <Field label="gbrain_min_version"><input className={inputClass} value={draft.gbrain_min_version} onChange={(event) => setDraft({ ...draft, gbrain_min_version: event.target.value })} /></Field>
                          </div> : null}
                        </section>

                        <EditorSection
                          title="页面类型"
                          description="添加新类型；与父包同名时按 gbrain 语义覆盖。"
                          count={draft.page_types.length}
                          onAdd={() => setPageTypeEditor({
                            index: null,
                            initial: { name: "", primitive: "concept", path_prefixes: [], aliases: [], extractable: false, expert_routing: false },
                          })}
                        >
                          {draft.page_types.length === 0 ? <p className="py-4 text-center text-xs text-gray-400">当前完全继承父包。</p> : null}
                          {draft.page_types.map((page, index) => (
                            <div key={`page-${index}`} className="flex items-center gap-2 rounded-xl border border-black/[0.055] bg-white p-2 transition hover:border-[#002fa7]/15">
                              <button
                                type="button"
                                onClick={() => setPageTypeEditor({ index, initial: clonePageType(page) })}
                                className="flex min-w-0 flex-1 items-center gap-3 rounded-lg px-2 py-2 text-left hover:bg-black/[0.02]"
                              >
                                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[#002fa7]/[0.07] text-[#002fa7]">
                                  <Pencil className="h-3.5 w-3.5" />
                                </span>
                                <span className="min-w-0 flex-1">
                                  <span className="flex flex-wrap items-center gap-2">
                                    <code className="truncate text-sm font-semibold text-gray-900">{page.name}</code>
                                    <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${parentPageTypeNames.has(page.name) ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700"}`}>
                                      {parentPageTypeNames.has(page.name) ? "覆盖父包" : "新增"}
                                    </span>
                                  </span>
                                  <span className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-gray-400">
                                    <span>primitive: <code>{page.primitive}</code></span>
                                    <span>{page.path_prefixes.length} 个路径前缀</span>
                                    <span>{page.aliases.length} 个别名</span>
                                  </span>
                                </span>
                                <ChevronRight className="h-4 w-4 shrink-0 text-gray-300" />
                              </button>
                              <div className="shrink-0">
                                <ReorderButtons index={index} count={draft.page_types.length} onMove={(delta) => { const target = index + delta; if (target < 0 || target >= draft.page_types.length) return; const page_types = [...draft.page_types]; [page_types[index], page_types[target]] = [page_types[target], page_types[index]]; setDraft({ ...draft, page_types }); }} />
                              </div>
                              <button type="button" onClick={() => setDraft({ ...draft, page_types: draft.page_types.filter((_, itemIndex) => itemIndex !== index) })} className="inline-flex h-9 shrink-0 items-center gap-1 rounded-xl px-3 text-xs text-red-500 hover:bg-red-50"><Trash2 className="h-3.5 w-3.5" /> 删除</button>
                            </div>
                          ))}
                        </EditorSection>

                        <EditorSection
                          title="关系类型"
                          description="维护 Wiki 中允许使用的关系及可选反向关系。"
                          count={draft.link_types.length}
                          onAdd={() => setLinkTypeEditor({ index: null, initial: { name: "" } })}
                        >
                          {draft.link_types.length === 0 ? <p className="py-4 text-center text-xs text-gray-400">当前完全继承父包。</p> : null}
                          {draft.link_types.map((link, index) => (
                            <div key={`link-${index}`} className="flex items-center gap-2 rounded-xl border border-black/[0.055] bg-white p-2 transition hover:border-violet-500/15">
                              <button
                                type="button"
                                onClick={() => setLinkTypeEditor({ index, initial: cloneLinkType(link) })}
                                className="flex min-w-0 flex-1 items-center gap-3 rounded-lg px-2 py-2 text-left hover:bg-black/[0.02]"
                              >
                                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-violet-500/[0.07] text-violet-600">
                                  <Pencil className="h-3.5 w-3.5" />
                                </span>
                                <span className="min-w-0 flex-1">
                                  <span className="flex flex-wrap items-center gap-2">
                                    <code className="truncate text-sm font-semibold text-gray-900">{link.name}</code>
                                    <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${parentLinkTypeNames.has(link.name) ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700"}`}>
                                      {parentLinkTypeNames.has(link.name) ? "覆盖父包" : "新增"}
                                    </span>
                                  </span>
                                  <span className="mt-1 block text-[10px] text-gray-400">
                                    {link.inverse ? <>反向关系：<code>{link.inverse}</code></> : "未设置反向关系"}
                                  </span>
                                </span>
                                <ChevronRight className="h-4 w-4 shrink-0 text-gray-300" />
                              </button>
                              <div className="shrink-0">
                                <ReorderButtons index={index} count={draft.link_types.length} onMove={(delta) => { const target = index + delta; if (target < 0 || target >= draft.link_types.length) return; const link_types = [...draft.link_types]; [link_types[index], link_types[target]] = [link_types[target], link_types[index]]; setDraft({ ...draft, link_types }); }} />
                              </div>
                              <button type="button" onClick={() => setDraft({ ...draft, link_types: draft.link_types.filter((_, itemIndex) => itemIndex !== index) })} className="inline-flex h-9 shrink-0 items-center gap-1 rounded-xl px-3 text-xs text-red-500 hover:bg-red-50"><Trash2 className="h-3.5 w-3.5" /> 删除</button>
                            </div>
                          ))}
                        </EditorSection>
                      </div>
                    ) : null}

                    {tab === "resolved" ? (
                      <div>
                        <h2 className="text-sm font-semibold text-gray-900">解析后的有效 Schema</h2>
                        <p className="mt-1 text-xs leading-5 text-gray-500">按 gbrain 的继承、覆盖和借用语义合并，编译和完整性检查使用这一视图。</p>
                        <div className="mt-4 grid gap-3 sm:grid-cols-2">
                          <div className="rounded-2xl border border-black/[0.06] bg-white p-4"><p className="text-2xl font-semibold text-gray-900">{preview?.resolved.manifest.page_types.length ?? bundle.resolved.manifest.page_types.length}</p><p className="mt-1 text-xs text-gray-400">Page Types</p></div>
                          <div className="rounded-2xl border border-black/[0.06] bg-white p-4"><p className="text-2xl font-semibold text-gray-900">{preview?.resolved.manifest.link_types.length ?? bundle.resolved.manifest.link_types.length}</p><p className="mt-1 text-xs text-gray-400">Link Types</p></div>
                        </div>
                        <div className="mt-4 rounded-2xl border border-black/[0.06] bg-white p-4">
                          <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Resolved Page Types</p>
                          <div className="mt-3 flex flex-wrap gap-2">
                            {(preview?.resolved.manifest.page_types ?? bundle.resolved.manifest.page_types).map((item) => (
                              <span key={item.name} className="rounded-lg border border-[#002fa7]/10 bg-[#002fa7]/[0.04] px-2.5 py-1.5 text-xs text-[#002fa7]">{item.name} <span className="text-[#002fa7]/50">· {item.primitive}</span></span>
                            ))}
                          </div>
                        </div>
                      </div>
                    ) : null}
                  </div>
                </section>

                {tab !== "workflow" && rawPreviewOpen ? <aside className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-[24px] border border-black/[0.06] bg-white">
                  <div className="flex shrink-0 items-center gap-2 border-b border-black/[0.055] px-3 py-2">
                    <Code2 className="h-4 w-4 text-gray-400" />
                    <span className="mr-auto text-xs font-semibold text-gray-700">原始文件预览</span>
                    <select value={rawTarget} onChange={(event) => setRawTarget(event.target.value as RawTarget)} className="h-8 rounded-lg border border-black/[0.07] bg-white px-2 text-[11px] text-gray-600 outline-none">
                      <option value="parent">官方父包</option>
                      <option value="custom">自定义包</option>
                      <option value="resolved">合并终包</option>
                      <option value="brain">工作区配置</option>
                    </select>
                    <button
                      type="button"
                      onClick={() => setRawPreviewOpen(false)}
                      className="rounded-lg p-2 text-gray-400 hover:bg-black/[0.04] hover:text-gray-700"
                      aria-label="收起原始文件预览"
                      title="收起原始文件预览"
                    >
                      <PanelRightClose className="h-4 w-4" />
                    </button>
                  </div>
                  <div className="shrink-0 border-b border-black/[0.04] bg-black/[0.018] px-3 py-2 font-mono text-[10px] text-gray-400">
                    {rawTarget === "parent" ? selectedBuiltin?.name : rawTarget === "custom" ? bundle.custom.path : rawTarget === "brain" ? bundle.brain_schema.path : "generated://resolved-pack.yaml"}
                  </div>
                  <div className="min-h-0 flex-1">
                    <MonacoEditor
                      height="100%"
                      language="yaml"
                      theme="vs"
                      value={rawText}
                      options={{
                        readOnly: true,
                        domReadOnly: true,
                        minimap: { enabled: false },
                        fontSize: 12,
                        lineHeight: 19,
                        lineNumbers: "on",
                        wordWrap: "on",
                        scrollBeyondLastLine: false,
                        renderLineHighlight: "none",
                        overviewRulerBorder: false,
                        automaticLayout: true,
                        padding: { top: 10, bottom: 10 },
                        fontFamily: "'SF Mono','JetBrains Mono','Fira Code',Consolas,monospace",
                      }}
                    />
                  </div>
                </aside> : null}
              </div>
            </div>
          )}
        </main>
      </div>
      {pageTypeEditor && draft ? (
        <PageTypeEditorModal
          key={pageTypeEditor.index === null ? "new-page-type" : `page-type-${pageTypeEditor.index}`}
          target={pageTypeEditor}
          existingNames={new Set(
            draft.page_types
              .filter((_, index) => index !== pageTypeEditor.index)
              .map((item) => item.name),
          )}
          onClose={() => setPageTypeEditor(null)}
          onSave={savePageTypeDetail}
        />
      ) : null}
      {linkTypeEditor && draft ? (
        <LinkTypeEditorModal
          key={linkTypeEditor.index === null ? "new-link-type" : `link-type-${linkTypeEditor.index}`}
          target={linkTypeEditor}
          existingNames={new Set(
            draft.link_types
              .filter((_, index) => index !== linkTypeEditor.index)
              .map((item) => item.name),
          )}
          onClose={() => setLinkTypeEditor(null)}
          onSave={saveLinkTypeDetail}
        />
      ) : null}
    </div>
  );
}
