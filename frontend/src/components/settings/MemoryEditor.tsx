"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import {
  getFileTokenCounts,
  listProjects,
  readFile,
  saveFile,
  type ProjectMeta,
} from "@/lib/api";
import {
  AlertCircle,
  CheckCircle2,
  FolderOpen,
  Globe2,
  Loader2,
  RefreshCw,
  Save,
} from "lucide-react";
import "@/lib/monaco-config";

const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-sm text-gray-400">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" />正在加载编辑器…
    </div>
  ),
});

type MemoryScope = {
  id: string;
  label: string;
  description: string;
  path: string;
  kind: "global" | "project";
};

const GLOBAL_MEMORY: MemoryScope = {
  id: "global",
  label: "全局记忆",
  description: "用于未绑定项目的 Agent 运行",
  path: "memory/global/MEMORY.md",
  kind: "global",
};

function projectMemory(project: ProjectMeta): MemoryScope {
  return {
    id: project.project_id,
    label: project.name,
    description: "仅用于该项目的 Agent 运行",
    path: `memory/projects/${project.project_id}/MEMORY.md`,
    kind: "project",
  };
}

export default function MemoryEditor() {
  const [projects, setProjects] = useState<ProjectMeta[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [selectedPath, setSelectedPath] = useState(GLOBAL_MEMORY.path);
  const [content, setContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [loadNotice, setLoadNotice] = useState<"missing" | "error" | null>(null);
  const [saveStatus, setSaveStatus] = useState<"idle" | "saved" | "error">("idle");
  const [tokenCounts, setTokenCounts] = useState<Record<string, number>>({});

  const scopes = useMemo(
    () => [GLOBAL_MEMORY, ...projects.map(projectMemory)],
    [projects],
  );
  const selectedScope = scopes.find((scope) => scope.path === selectedPath) ?? GLOBAL_MEMORY;
  const isDirty = content !== originalContent;
  const selectedTokenCount = tokenCounts[selectedPath] ?? 0;

  const refreshTokenCounts = useCallback(async (paths: string[]) => {
    try {
      const data = await getFileTokenCounts(paths);
      const counts: Record<string, number> = {};
      for (const file of data.files) counts[file.path] = file.tokens;
      setTokenCounts(counts);
    } catch {
      // Token counts are informational and should never block editing.
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setProjectsLoading(true);
    listProjects()
      .then((items) => {
        if (!cancelled) setProjects(items);
      })
      .catch(() => {
        if (!cancelled) setProjects([]);
      })
      .finally(() => {
        if (!cancelled) setProjectsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    void refreshTokenCounts(scopes.map((scope) => scope.path));
  }, [refreshTokenCounts, scopes]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadNotice(null);
    setSaveStatus("idle");
    readFile(selectedPath)
      .then((text) => {
        if (cancelled) return;
        setContent(text);
        setOriginalContent(text);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const missing = error instanceof Error && error.message.includes(": 404");
        setContent("");
        setOriginalContent("");
        setLoadNotice(missing ? "missing" : "error");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadVersion, selectedPath]);

  const selectScope = useCallback((path: string) => {
    if (path === selectedPath) return;
    if (isDirty && !window.confirm("当前 Memory 有未保存的修改，确定要切换范围吗？")) return;
    setSelectedPath(path);
  }, [isDirty, selectedPath]);

  const handleSave = useCallback(async () => {
    if (saving || !isDirty || loadNotice === "error") return;
    setSaving(true);
    setSaveStatus("idle");
    try {
      await saveFile(selectedPath, content);
      setOriginalContent(content);
      setLoadNotice(null);
      setSaveStatus("saved");
      await refreshTokenCounts(scopes.map((scope) => scope.path));
      window.setTimeout(() => setSaveStatus("idle"), 2000);
    } catch {
      setSaveStatus("error");
    } finally {
      setSaving(false);
    }
  }, [content, isDirty, loadNotice, refreshTokenCounts, saving, scopes, selectedPath]);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void handleSave();
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [handleSave]);

  return (
    <div className="flex h-full min-h-[560px] flex-col overflow-hidden rounded-2xl border border-black/[0.06] bg-white/70 shadow-sm backdrop-blur-xl">
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <aside className="flex max-h-52 w-full shrink-0 flex-col border-b border-black/[0.06] bg-slate-50/55 p-3 md:max-h-none md:w-64 md:border-b-0 md:border-r">
          <p className="px-2 pb-2 pt-1 text-[10px] font-semibold uppercase tracking-widest text-gray-400">
            记忆范围
          </p>
          <ScopeButton
            scope={GLOBAL_MEMORY}
            active={selectedPath === GLOBAL_MEMORY.path}
            tokens={tokenCounts[GLOBAL_MEMORY.path] ?? 0}
            onSelect={selectScope}
          />

          <div className="my-3 h-px bg-black/[0.05]" />
          <div className="mb-1 flex items-center justify-between px-2">
            <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-400">项目记忆</p>
            {!projectsLoading && (
              <span className="text-[10px] tabular-nums text-gray-400">{projects.length}</span>
            )}
          </div>
          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
            {projectsLoading ? (
              <div className="flex items-center gap-2 px-2 py-3 text-[11px] text-gray-400">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />正在加载项目…
              </div>
            ) : projects.length ? (
              projects.map((project) => {
                const scope = projectMemory(project);
                return (
                  <ScopeButton
                    key={scope.id}
                    scope={scope}
                    active={selectedPath === scope.path}
                    tokens={tokenCounts[scope.path] ?? 0}
                    onSelect={selectScope}
                  />
                );
              })
            ) : (
              <p className="px-2 py-3 text-[11px] leading-5 text-gray-400">暂无已注册项目。</p>
            )}
          </div>

          <div className="mt-3 rounded-xl border border-amber-100 bg-amber-50/70 p-3 text-[10px] leading-4 text-amber-800">
            不要在 Memory 中保存密钥、临时任务状态、完整对话或未经确认的推测。
          </div>
        </aside>

        <section className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-black/[0.06] bg-white/60 px-4 py-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                {selectedScope.kind === "global" ? (
                  <Globe2 className="h-4 w-4 shrink-0 text-[#002fa7]" />
                ) : (
                  <FolderOpen className="h-4 w-4 shrink-0 text-[#002fa7]" />
                )}
                <h3 className="truncate text-[13px] font-semibold text-gray-800">{selectedScope.label}</h3>
                <span className="shrink-0 rounded-full bg-[#002fa7]/[0.07] px-2 py-0.5 text-[9px] font-medium text-[#002fa7]">
                  {selectedTokenCount.toLocaleString()} Token
                </span>
                <span className="hidden shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-[9px] text-gray-500 sm:inline-flex">
                  {selectedScope.kind === "global" ? "全局作用域" : "项目作用域"}
                </span>
                {isDirty && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" />}
              </div>
              <p className="mt-0.5 truncate pl-6 text-[10px] text-gray-400">{selectedScope.description}</p>
            </div>
            <div className="flex items-center gap-1.5">
              {saveStatus === "saved" && (
                <span className="flex items-center gap-1 text-[10px] text-emerald-600">
                  <CheckCircle2 className="h-3.5 w-3.5" />已保存
                </span>
              )}
              {saveStatus === "error" && (
                <span className="flex items-center gap-1 text-[10px] text-red-600">
                  <AlertCircle className="h-3.5 w-3.5" />保存失败
                </span>
              )}
              <button
                type="button"
                onClick={() => {
                  if (isDirty && !window.confirm("确定放弃当前未保存的修改并重新加载吗？")) return;
                  setReloadVersion((version) => version + 1);
                }}
                disabled={loading || saving}
                className="rounded-lg p-2 text-gray-400 transition-colors hover:bg-black/[0.04] hover:text-gray-700 disabled:opacity-40"
                title="重新加载"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
              </button>
              <button
                type="button"
                onClick={() => void handleSave()}
                disabled={saving || !isDirty || loadNotice === "error"}
                className="flex items-center gap-1.5 rounded-lg bg-[#002fa7] px-3 py-2 text-[11px] font-medium text-white transition-all hover:bg-[#001f7a] active:scale-95 disabled:opacity-30"
                title="保存 Memory（⌘/Ctrl + S）"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                保存 Memory
              </button>
            </div>
          </div>

          {loadNotice && (
            <div className={`mx-4 mt-3 flex items-start gap-2 rounded-lg border px-3 py-2 text-[11px] leading-5 ${
              loadNotice === "missing"
                ? "border-blue-100 bg-blue-50 text-blue-700"
                : "border-red-100 bg-red-50 text-red-700"
            }`}>
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {loadNotice === "missing"
                ? "这个范围还没有 MEMORY.md。输入内容并保存即可创建。"
                : "无法读取这个 Memory。请检查后端状态后重新加载；为避免覆盖，当前禁止保存。"}
            </div>
          )}

          <div className="min-h-0 flex-1">
            {loading ? (
              <div className="flex h-full items-center justify-center text-sm text-gray-400">
                <Loader2 className="mr-2 h-5 w-5 animate-spin" />正在读取 Memory…
              </div>
            ) : (
              <MonacoEditor
                height="100%"
                language="markdown"
                value={content}
                theme="vs"
                onChange={(value) => setContent(value || "")}
                options={{
                  minimap: { enabled: false },
                  fontSize: 13,
                  lineNumbers: "on",
                  wordWrap: "on",
                  scrollBeyondLastLine: false,
                  padding: { top: 14, bottom: 14 },
                  renderLineHighlight: "none",
                  overviewRulerBorder: false,
                  hideCursorInOverviewRuler: true,
                  automaticLayout: true,
                  fontFamily: "'SF Mono','JetBrains Mono','Fira Code',Consolas,monospace",
                  lineHeight: 21,
                  cursorBlinking: "smooth",
                  smoothScrolling: true,
                  readOnly: loadNotice === "error",
                }}
              />
            )}
          </div>
          <footer className="flex shrink-0 items-center justify-between border-t border-black/[0.05] bg-slate-50/50 px-4 py-2 text-[10px] text-gray-400">
            <span>MEMORY.md · Markdown</span>
            <span>⌘/Ctrl + S 保存</span>
          </footer>
        </section>
      </div>
    </div>
  );
}

function ScopeButton({
  scope,
  active,
  tokens,
  onSelect,
}: {
  scope: MemoryScope;
  active: boolean;
  tokens: number;
  onSelect: (path: string) => void;
}) {
  const Icon = scope.kind === "global" ? Globe2 : FolderOpen;
  return (
    <button
      type="button"
      onClick={() => onSelect(scope.path)}
      className={`relative flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2.5 text-left transition-all ${
        active
          ? "bg-white text-gray-900 shadow-sm ring-1 ring-black/[0.04]"
          : "text-gray-500 hover:bg-white/65 hover:text-gray-800"
      }`}
    >
      {active && <span className="absolute inset-y-2 left-0 w-[3px] rounded-r-full bg-[#002fa7]" />}
      <Icon className="h-3.5 w-3.5 shrink-0 text-[#002fa7]" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12px] font-medium">{scope.label}</span>
        <span className="mt-0.5 block truncate text-[9px] text-gray-400">{tokens.toLocaleString()} Token</span>
      </span>
    </button>
  );
}
