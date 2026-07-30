"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronRight,
  Code2,
  DatabaseZap,
  FileCode2,
  Layers3,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import {
  getBrainSchemaBundle,
  getBrainSchemaCatalog,
  initializeBrainSchema,
  previewBrainCustomSchema,
  saveBrainCustomSchema,
  type BrainSchemaBundle,
  type BrainSchemaPreview,
  type GbrainFrontmatterLink,
  type GbrainLinkType,
  type GbrainPageType,
  type GbrainPrimitive,
  type GbrainSchemaCatalog,
  type GbrainSchemaCatalogPack,
  type GbrainSchemaPackManifest,
} from "@/lib/api";
import "@/lib/monaco-config";
import { useApp } from "@/lib/store";
import AdvancedSchemaEditor, { PageTypeAdvancedEditor, ReorderButtons, StringListEditor } from "./advanced-schema-editor";

const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-sm text-gray-400">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 加载 YAML 预览…
    </div>
  ),
});

type MainTab = "builtins" | "custom" | "resolved";
type RawTarget = "custom" | "resolved" | "parent" | "brain";

const PRIMITIVES: GbrainPrimitive[] = ["entity", "media", "temporal", "annotation", "concept"];
const inputClass =
  "h-9 w-full rounded-xl border border-black/[0.08] bg-white px-3 text-sm text-gray-800 outline-none transition focus:border-[#002fa7]/35 focus:ring-2 focus:ring-[#002fa7]/10";

function cloneManifest(value: GbrainSchemaPackManifest): GbrainSchemaPackManifest {
  return JSON.parse(JSON.stringify(value)) as GbrainSchemaPackManifest;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block min-w-0">
      <span className="mb-1.5 flex items-baseline gap-2 text-xs font-medium text-gray-700">
        {label}
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
      <div className="flex items-center gap-3 px-4 py-3">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="rounded-lg p-1 text-gray-400 hover:bg-black/[0.04] hover:text-gray-700"
          aria-label={open ? `收起${title}` : `展开${title}`}
        >
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
            <span className="rounded-full bg-black/[0.045] px-2 py-0.5 text-[10px] text-gray-500">{count}</span>
          </div>
          <p className="mt-0.5 text-[11px] text-gray-400">{description}</p>
        </div>
        <button
          type="button"
          onClick={onAdd}
          className="inline-flex h-8 items-center gap-1 rounded-full bg-[#002fa7]/10 px-3 text-xs font-medium text-[#002fa7] hover:bg-[#002fa7]/15"
        >
          <Plus className="h-3.5 w-3.5" /> 添加
        </button>
      </div>
      {open ? <div className="space-y-3 border-t border-black/[0.05] p-4">{children}</div> : null}
    </section>
  );
}

function BuiltinCard({
  pack,
  active,
  currentParent,
  onInspect,
  onUse,
}: {
  pack: GbrainSchemaCatalogPack;
  active: boolean;
  currentParent: boolean;
  onInspect: () => void;
  onUse: () => void;
}) {
  return (
    <article
      className={`rounded-2xl border p-4 transition ${
        active ? "border-[#002fa7]/30 bg-[#002fa7]/[0.035]" : "border-black/[0.06] bg-white hover:border-black/[0.12]"
      }`}
    >
      <button type="button" onClick={onInspect} className="block w-full text-left">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <h3 className="truncate text-sm font-semibold text-gray-900">{pack.name}</h3>
              {pack.recommended ? (
                <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">推荐</span>
              ) : null}
              {pack.legacy ? (
                <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">旧基线</span>
              ) : null}
            </div>
            <p className="mt-1 line-clamp-2 text-xs leading-5 text-gray-500">{pack.description || "无描述"}</p>
          </div>
          <span className="shrink-0 font-mono text-[10px] text-gray-400">v{pack.version}</span>
        </div>
        <div className="mt-3 flex flex-wrap gap-2 text-[10px] text-gray-500">
          <span className="rounded-lg bg-black/[0.035] px-2 py-1">{pack.page_type_count} page types</span>
          <span className="rounded-lg bg-black/[0.035] px-2 py-1">{pack.link_type_count} link types</span>
          {pack.extends ? <span className="rounded-lg bg-black/[0.035] px-2 py-1">extends {pack.extends}</span> : null}
        </div>
      </button>
      <button
        type="button"
        onClick={onUse}
        className={`mt-3 inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-xl text-xs font-medium transition ${
          currentParent
            ? "bg-emerald-50 text-emerald-700"
            : "border border-black/[0.07] bg-white text-gray-700 hover:border-[#002fa7]/20 hover:text-[#002fa7]"
        }`}
      >
        {currentParent ? <Check className="h-3.5 w-3.5" /> : <Layers3 className="h-3.5 w-3.5" />}
        {currentParent ? "当前父包" : "设为父包"}
      </button>
    </article>
  );
}

export default function BrainSchemaPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [catalog, setCatalog] = useState<GbrainSchemaCatalog | null>(null);
  const [bundle, setBundle] = useState<BrainSchemaBundle | null>(null);
  const [draft, setDraft] = useState<GbrainSchemaPackManifest | null>(null);
  const [preview, setPreview] = useState<BrainSchemaPreview | null>(null);
  const [selectedPack, setSelectedPack] = useState<string>("gbrain-base-v2");
  const [tab, setTab] = useState<MainTab>("builtins");
  const [rawTarget, setRawTarget] = useState<RawTarget>("parent");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [initializing, setInitializing] = useState(false);
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => setMounted(true), []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextCatalog, nextBundle] = await Promise.all([getBrainSchemaCatalog(), getBrainSchemaBundle()]);
      setCatalog(nextCatalog);
      setBundle(nextBundle);
      if (nextBundle) {
        setDraft(cloneManifest(nextBundle.custom.manifest));
        setSelectedPack(nextBundle.custom.manifest.extends || nextCatalog.packs[0]?.name || "");
      } else {
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
      setTab("custom");
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

  const updatePage = useCallback((index: number, patch: Partial<GbrainPageType>) => {
    setDraft((current) => {
      if (!current) return current;
      const pageTypes = current.page_types.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      );
      return { ...current, page_types: pageTypes };
    });
  }, []);

  const updateLink = useCallback((index: number, patch: Partial<GbrainLinkType>) => {
    setDraft((current) => {
      if (!current) return current;
      const linkTypes = current.link_types.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      );
      return { ...current, link_types: linkTypes };
    });
  }, []);

  const updateFrontmatter = useCallback((index: number, patch: Partial<GbrainFrontmatterLink>) => {
    setDraft((current) => {
      if (!current) return current;
      const frontmatterLinks = current.frontmatter_links.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      );
      return { ...current, frontmatter_links: frontmatterLinks };
    });
  }, []);

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
                <h1 className="truncate text-lg font-semibold text-gray-950">LLM Wiki · Schema Studio</h1>
              </div>
              <p className="mt-0.5 truncate text-xs text-gray-500">官方 gbrain Schema 为基线，PuddingClaw 扩展包为唯一可编辑源</p>
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
          ) : error && !catalog ? (
            <div className="m-6 rounded-2xl border border-red-500/15 bg-red-50 p-4 text-sm text-red-700">{error}</div>
          ) : !bundle ? (
            <div className="flex flex-1 items-center justify-center p-6">
              <div className="max-w-lg rounded-[28px] border border-black/[0.06] bg-white/90 p-8 text-center shadow-sm">
                <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-[#002fa7]/10 text-[#002fa7]">
                  <DatabaseZap className="h-6 w-6" />
                </div>
                <h2 className="mt-5 text-xl font-semibold text-gray-950">初始化 LLM Wiki Brain</h2>
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
                      {validating ? (
                        <span className="inline-flex items-center text-gray-400"><Loader2 className="mr-1 h-3 w-3 animate-spin" />校验中</span>
                      ) : preview ? (
                        <span className="inline-flex items-center text-emerald-600"><Check className="mr-1 h-3 w-3" />{preview?.validation_mode === "structural" ? "结构有效 · 保存时运行 gbrain" : "官方格式有效"}</span>
                      ) : null}
                      {dirty ? <span className="h-2 w-2 rounded-full bg-amber-400" title="有未保存更改" /> : null}
                      {versionNeedsBump ? <span className="text-amber-600">请升级 SemVer</span> : null}
                    </div>
                  </nav>

                  <div className="min-h-0 flex-1 overflow-y-auto p-4">
                    {tab === "builtins" ? (
                      <div>
                        <div className="mb-4">
                          <h2 className="text-sm font-semibold text-gray-900">gbrain 内置 Schema Packs</h2>
                          <p className="mt-1 text-xs leading-5 text-gray-500">
                            内容直接来自当前安装的 gbrain。选择父包只修改自定义 pack 的 <code>extends</code>。
                          </p>
                        </div>
                        <div className="grid gap-3 xl:grid-cols-2">
                          {catalog?.packs.map((pack) => (
                            <BuiltinCard
                              key={pack.name}
                              pack={pack}
                              active={selectedPack === pack.name}
                              currentParent={draft?.extends === pack.name}
                              onInspect={() => {
                                setSelectedPack(pack.name);
                                setRawTarget("parent");
                              }}
                              onUse={() => chooseParent(pack.name)}
                            />
                          ))}
                        </div>
                        {selectedBuiltin ? (
                          <div className="mt-4 rounded-2xl border border-black/[0.06] bg-white p-4">
                            <h3 className="text-sm font-semibold text-gray-900">{selectedBuiltin.name} 结构摘要</h3>
                            <div className="mt-3 grid gap-4 lg:grid-cols-2">
                              <div>
                                <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Page Types</p>
                                <div className="mt-2 flex flex-wrap gap-1.5">
                                  {selectedBuiltin.manifest.page_types.map((item) => (
                                    <span key={item.name} className="rounded-lg bg-[#002fa7]/[0.06] px-2 py-1 text-[10px] text-[#002fa7]">
                                      {item.name} · {item.primitive}
                                    </span>
                                  ))}
                                </div>
                              </div>
                              <div>
                                <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Link Types</p>
                                <div className="mt-2 flex flex-wrap gap-1.5">
                                  {selectedBuiltin.manifest.link_types.map((item) => (
                                    <span key={item.name} className="rounded-lg bg-violet-500/[0.06] px-2 py-1 text-[10px] text-violet-700">
                                      {item.name}{item.inverse ? ` ↔ ${item.inverse}` : ""}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            </div>
                          </div>
                        ) : null}
                      </div>
                    ) : null}

                    {tab === "custom" && draft ? (
                      <div className="space-y-4">
                        <section className="rounded-2xl border border-black/[0.06] bg-white p-4">
                          <div className="mb-3">
                            <h2 className="text-sm font-semibold text-gray-900">官方 Manifest 元数据</h2>
                            <p className="mt-1 text-[11px] text-gray-400">字段名和数据形状与 gbrain-schema-pack-v1 保持一致。</p>
                          </div>
                          <div className="grid gap-3 md:grid-cols-2">
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
                          </div>
                        </section>

                        <EditorSection
                          title="Page Types"
                          description="添加新类型；与父包同名时按 gbrain 语义覆盖。"
                          count={draft.page_types.length}
                          onAdd={() => setDraft({
                            ...draft,
                            page_types: [...draft.page_types, { name: "", primitive: "concept", path_prefixes: [], aliases: [], extractable: false, expert_routing: false }],
                          })}
                        >
                          {draft.page_types.length === 0 ? <p className="py-4 text-center text-xs text-gray-400">当前完全继承父包。</p> : null}
                          {draft.page_types.map((page, index) => (
                            <div key={`page-${index}`} className="rounded-xl bg-black/[0.025] p-3">
                              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                                <Field label="name"><input className={inputClass} value={page.name} onChange={(event) => updatePage(index, { name: event.target.value })} /></Field>
                                <Field label="primitive">
                                  <select className={inputClass} value={page.primitive} onChange={(event) => updatePage(index, { primitive: event.target.value as GbrainPrimitive })}>
                                    {PRIMITIVES.map((primitive) => <option key={primitive}>{primitive}</option>)}
                                  </select>
                                </Field>
                                <Field label="path_prefixes"><StringListEditor value={page.path_prefixes} onChange={(path_prefixes) => updatePage(index, { path_prefixes })} /></Field>
                                <Field label="aliases"><StringListEditor value={page.aliases} onChange={(aliases) => updatePage(index, { aliases })} /></Field>
                                <div className="flex items-end gap-4 pb-2 text-xs text-gray-600">
                                  <label className="inline-flex items-center gap-2"><input type="checkbox" checked={page.expert_routing} onChange={(event) => updatePage(index, { expert_routing: event.target.checked })} /> expert_routing</label>
                                </div>
                                <div className="flex items-end justify-end gap-2">
                                  <ReorderButtons index={index} count={draft.page_types.length} onMove={(delta) => { const target = index + delta; if (target < 0 || target >= draft.page_types.length) return; const page_types = [...draft.page_types]; [page_types[index], page_types[target]] = [page_types[target], page_types[index]]; setDraft({ ...draft, page_types }); }} />
                                  <button type="button" onClick={() => setDraft({ ...draft, page_types: draft.page_types.filter((_, itemIndex) => itemIndex !== index) })} className="inline-flex h-9 items-center gap-1 rounded-xl px-3 text-xs text-red-500 hover:bg-red-50"><Trash2 className="h-3.5 w-3.5" /> 删除</button>
                                </div>
                              </div>
                              <PageTypeAdvancedEditor page={page} onChange={(patch) => updatePage(index, patch)} />
                            </div>
                          ))}
                        </EditorSection>

                        <EditorSection
                          title="Link Types"
                          description="定义链接、反向链接及可选推断规则。"
                          count={draft.link_types.length}
                          onAdd={() => setDraft({ ...draft, link_types: [...draft.link_types, { name: "" }] })}
                        >
                          {draft.link_types.length === 0 ? <p className="py-4 text-center text-xs text-gray-400">当前完全继承父包。</p> : null}
                          {draft.link_types.map((link, index) => (
                            <div key={`link-${index}`} className="rounded-xl bg-black/[0.025] p-3">
                              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                                <Field label="name"><input className={inputClass} value={link.name} onChange={(event) => updateLink(index, { name: event.target.value })} /></Field>
                                <Field label="inverse"><input className={inputClass} value={link.inverse || ""} onChange={(event) => updateLink(index, { inverse: event.target.value || undefined })} /></Field>
                                <Field label="inference.regex"><input className={inputClass} value={link.inference?.regex || ""} onChange={(event) => updateLink(index, { inference: { ...link.inference, regex: event.target.value || undefined } })} /></Field>
                                <Field label="inference.page_type"><input className={inputClass} value={link.inference?.page_type || ""} onChange={(event) => updateLink(index, { inference: { ...link.inference, page_type: event.target.value || undefined } })} /></Field>
                                <Field label="inference.target_type"><input className={inputClass} value={link.inference?.target_type || ""} onChange={(event) => updateLink(index, { inference: { ...link.inference, target_type: event.target.value || undefined } })} /></Field>
                                <div className="flex items-end justify-end">
                                  <button type="button" onClick={() => setDraft({ ...draft, link_types: draft.link_types.filter((_, itemIndex) => itemIndex !== index) })} className="inline-flex h-9 items-center gap-1 rounded-xl px-3 text-xs text-red-500 hover:bg-red-50"><Trash2 className="h-3.5 w-3.5" /> 删除</button>
                                </div>
                              </div>
                            </div>
                          ))}
                        </EditorSection>

                        <EditorSection
                          title="Frontmatter Link Rules"
                          description="官方 frontmatter_links：把页面字段映射为有类型的图链接。"
                          count={draft.frontmatter_links.length}
                          onAdd={() => setDraft({ ...draft, frontmatter_links: [...draft.frontmatter_links, { page_type: "", fields: [], link_type: "" }] })}
                        >
                          {draft.frontmatter_links.length === 0 ? <p className="py-4 text-center text-xs text-gray-400">尚未添加自定义 frontmatter 链接规则。</p> : null}
                          {draft.frontmatter_links.map((rule, index) => (
                            <div key={`frontmatter-${index}`} className="grid gap-3 rounded-xl bg-black/[0.025] p-3 md:grid-cols-2 xl:grid-cols-[1fr_1.5fr_1fr_auto]">
                              <Field label="page_type"><input className={inputClass} value={rule.page_type} onChange={(event) => updateFrontmatter(index, { page_type: event.target.value })} /></Field>
                              <Field label="fields"><StringListEditor value={rule.fields} onChange={(fields) => updateFrontmatter(index, { fields })} /></Field>
                              <Field label="link_type"><input className={inputClass} value={rule.link_type} onChange={(event) => updateFrontmatter(index, { link_type: event.target.value })} /></Field>
                              <button type="button" onClick={() => setDraft({ ...draft, frontmatter_links: draft.frontmatter_links.filter((_, itemIndex) => itemIndex !== index) })} className="mt-5 inline-flex h-9 items-center gap-1 rounded-xl px-3 text-xs text-red-500 hover:bg-red-50"><Trash2 className="h-3.5 w-3.5" /> 删除</button>
                            </div>
                          ))}
                        </EditorSection>

                        <section className="rounded-2xl border border-black/[0.06] bg-white p-4">
                          <h3 className="text-sm font-semibold text-gray-900">其他官方规则</h3>
                          <p className="mt-1 text-[11px] text-gray-400">takes_kinds 以及下方高级区均按 gbrain-schema-pack-v1 的官方字段形状编辑。</p>
                          <div className="mt-3">
                            <Field label="takes_kinds">
                              <StringListEditor value={draft.takes_kinds} onChange={(takes_kinds) => setDraft({ ...draft, takes_kinds })} />
                            </Field>
                          </div>
                        </section>
                        <AdvancedSchemaEditor draft={draft} onChange={setDraft} />
                      </div>
                    ) : null}

                    {tab === "resolved" ? (
                      <div>
                        <h2 className="text-sm font-semibold text-gray-900">解析后的有效 Schema</h2>
                        <p className="mt-1 text-xs leading-5 text-gray-500">按 gbrain 的继承、覆盖和借用语义合并，编译和 Lint 使用这一视图。</p>
                        <div className="mt-4 grid gap-3 sm:grid-cols-3">
                          <div className="rounded-2xl border border-black/[0.06] bg-white p-4"><p className="text-2xl font-semibold text-gray-900">{preview?.resolved.manifest.page_types.length ?? bundle.resolved.manifest.page_types.length}</p><p className="mt-1 text-xs text-gray-400">Page Types</p></div>
                          <div className="rounded-2xl border border-black/[0.06] bg-white p-4"><p className="text-2xl font-semibold text-gray-900">{preview?.resolved.manifest.link_types.length ?? bundle.resolved.manifest.link_types.length}</p><p className="mt-1 text-xs text-gray-400">Link Types</p></div>
                          <div className="rounded-2xl border border-black/[0.06] bg-white p-4"><p className="text-2xl font-semibold text-gray-900">{preview?.resolved.manifest.frontmatter_links.length ?? bundle.resolved.manifest.frontmatter_links.length}</p><p className="mt-1 text-xs text-gray-400">Frontmatter Rules</p></div>
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

                <aside className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-[24px] border border-black/[0.06] bg-white">
                  <div className="flex shrink-0 items-center gap-2 border-b border-black/[0.055] px-3 py-2">
                    <Code2 className="h-4 w-4 text-gray-400" />
                    <span className="mr-auto text-xs font-semibold text-gray-700">原始文件预览</span>
                    <select value={rawTarget} onChange={(event) => setRawTarget(event.target.value as RawTarget)} className="h-8 rounded-lg border border-black/[0.07] bg-white px-2 text-[11px] text-gray-600 outline-none">
                      <option value="parent">官方父包</option>
                      <option value="custom">自定义 pack.yaml</option>
                      <option value="resolved">合并后 Schema</option>
                      <option value="brain">brain.schema.yaml</option>
                    </select>
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
                </aside>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
