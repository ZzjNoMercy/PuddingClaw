"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useApp } from "@/lib/store";
import { readFile, saveFile, listSkills, getFileTokenCounts } from "@/lib/api";
import {
  Save,
  FileText,
  Loader2,
  CheckCircle2,
  AlertCircle,
  Brain,
  Zap,
  Sparkles,
  Maximize2,
  Minimize2,
  Settings2,
  Server,
} from "lucide-react";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import "@/lib/monaco-config";

const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full text-gray-400 text-sm">
      <Loader2 className="w-4 h-4 animate-spin mr-2" />Loading editor...
    </div>
  ),
});

// ── Memory files config ─────────────────────────────────
const MEMORY_FILES = [
  { label: "MEMORY.md", path: "memory/MEMORY.md", icon: Brain, color: "#7c3aed" },
  { label: "SOUL.md", path: "workspace/SOUL.md", icon: Sparkles, color: "#f59e0b" },
  { label: "IDENTITY.md", path: "workspace/IDENTITY.md", icon: FileText, color: "#6b7280" },
  { label: "USER.md", path: "workspace/USER.md", icon: FileText, color: "#6b7280" },
  { label: "AGENTS.md", path: "workspace/AGENTS.md", icon: FileText, color: "#10b981" },
  { label: "SKILLS_SNAPSHOT.md", path: "SKILLS_SNAPSHOT.md", icon: Zap, color: "#f59e0b" },
];

export default function InspectorPanel() {
  const {
    inspectorFile,
    setInspectorFile,
    rightTab,
    setRightTab,
    expandedFile,
    setExpandedFile,
    mcpServers,
  } = useApp();

  const router = useRouter();
  const [content, setContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<"idle" | "saved" | "error">("idle");
  const [skills, setSkills] = useState<Array<{ name: string; path: string; description: string }>>([]);
  const [activeSkills, setActiveSkills] = useState<Array<{ name: string; description: string }>>([]);
  const [tokenCounts, setTokenCounts] = useState<Record<string, number>>({});
  const editorRef = useRef<unknown>(null);

  // Load skills on mount
  useEffect(() => {
    listSkills()
      .then(setSkills)
      .catch(() => setSkills([]));
  }, []);

  // Load active skills when Skills tab is opened
  useEffect(() => {
    if (rightTab === "skills") {
      const API_BASE = `http://${window.location.hostname}:9100/api`;
      fetch(`${API_BASE}/skills/active`)
        .then(res => res.json())
        .then(data => setActiveSkills(data.skills || []))
        .catch(() => setActiveSkills([]));
    }
  }, [rightTab]);

  // Load token counts for memory files
  useEffect(() => {
    if (rightTab === "memory") {
      const paths = MEMORY_FILES.map((f) => f.path);
      getFileTokenCounts(paths)
        .then((data) => {
          const counts: Record<string, number> = {};
          for (const f of data.files) {
            counts[f.path] = f.tokens;
          }
          setTokenCounts(counts);
        })
        .catch(() => {});
    }
  }, [rightTab]);

  // Load file content when selected
  useEffect(() => {
    if (!inspectorFile) {
      setContent("");
      setOriginalContent("");
      return;
    }
    setLoading(true);
    setSaveStatus("idle");
    readFile(inspectorFile)
      .then((t) => {
        setContent(t);
        setOriginalContent(t);
      })
      .catch(() => {
        setContent("# Error loading file");
        setOriginalContent("");
      })
      .finally(() => setLoading(false));
  }, [inspectorFile]);

  const handleSave = useCallback(async () => {
    if (!inspectorFile || saving) return;
    setSaving(true);
    setSaveStatus("idle");
    try {
      await saveFile(inspectorFile, content);
      setOriginalContent(content);
      setSaveStatus("saved");
      setTimeout(() => setSaveStatus("idle"), 2000);
    } catch {
      setSaveStatus("error");
    } finally {
      setSaving(false);
    }
  }, [inspectorFile, content, saving]);

  // Ctrl+S shortcut
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        handleSave();
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [handleSave]);

  const isDirty = content !== originalContent;
  const fileExt = inspectorFile?.split(".").pop() || "md";
  const language = fileExt === "md" ? "markdown" : fileExt === "json" ? "json" : "markdown";
  const fileName = inspectorFile?.split("/").pop() || "";

  return (
    <div className="flex flex-col h-full">
      {/* Tab bar */}
      <div className="flex gap-0.5 p-2 pb-0 shrink-0">
        {(["memory", "skills", "mcp"] as const).map((tab) => {
          const active = rightTab === tab;
          const Icon =
            tab === "memory" ? Brain : tab === "skills" ? Zap : Server;
          const color =
            tab === "memory" ? "#7c3aed" : tab === "skills" ? "#f59e0b" : "#0ea5e9";
          const label =
            tab === "memory" ? "Memory" : tab === "skills" ? "Skills" : "MCP";
          return (
            <button
              key={tab}
              onClick={() => setRightTab(tab)}
              className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg text-[11px] font-medium transition-all ${
                active
                  ? "bg-white/70 shadow-sm text-gray-800"
                  : "text-gray-400 hover:text-gray-600 hover:bg-white/30"
              }`}
            >
              <Icon className="w-3.5 h-3.5" style={active ? { color } : {}} />
              {label}
            </button>
          );
        })}
      </div>

      <div className="mx-3 my-1.5 h-px bg-black/[0.04]" />

      {/* File list (hidden when expanded) */}
      {!expandedFile && (
        <div className="shrink-0 overflow-y-auto px-1.5 max-h-[35%]">
          {rightTab === "memory" ? (
            <FileList files={MEMORY_FILES} onSelect={setInspectorFile} selectedPath={inspectorFile} tokenCounts={tokenCounts} />
          ) : rightTab === "skills" ? (
            <SkillsFileList
              skills={skills}
              activeSkills={activeSkills}
              onSelect={setInspectorFile}
              selectedPath={inspectorFile}
              onNavigateToConfig={() => router.push("/skills")}
            />
          ) : (
            <McpServerList servers={mcpServers} />
          )}
        </div>
      )}

      {/* Editor area */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {inspectorFile ? (
          <>
            {/* Editor toolbar */}
            <div className="shrink-0 flex items-center justify-between px-3 py-1.5 border-t border-b border-black/[0.04] bg-white/60 backdrop-blur-sm">
              <div className="flex items-center gap-2 min-w-0">
                <FileText className="w-3.5 h-3.5 text-[#ff6723] shrink-0" />
                <div className="min-w-0">
                  <div className="text-[11px] font-semibold text-gray-700 truncate flex items-center gap-1.5">
                    {fileName}
                    {isDirty && <span className="w-1.5 h-1.5 bg-amber-400 rounded-full shrink-0" />}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-1">
                {saveStatus === "saved" && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" />}
                {saveStatus === "error" && <AlertCircle className="w-3.5 h-3.5 text-red-500" />}
                <button
                  onClick={handleSave}
                  disabled={saving || !isDirty}
                  className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-medium text-white bg-[#ff6723] disabled:opacity-25 hover:bg-[#e55a1b] transition-all active:scale-95"
                  title="Save (Ctrl+S)"
                >
                  {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
                  Save
                </button>
                <button
                  onClick={() => setExpandedFile(!expandedFile)}
                  className="p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-black/[0.04] transition-colors"
                  title={expandedFile ? "Collapse" : "Expand"}
                >
                  {expandedFile ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
                </button>
              </div>
            </div>

            {/* Monaco editor */}
            <div className="flex-1">
              {loading ? (
                <div className="flex items-center justify-center h-full">
                  <Loader2 className="w-5 h-5 animate-spin text-gray-400" />
                </div>
              ) : (
                <MonacoEditor
                  height="100%"
                  language={language}
                  value={content}
                  theme="vs"
                  onChange={(val) => setContent(val || "")}
                  onMount={(editor) => {
                    editorRef.current = editor;
                  }}
                  options={{
                    minimap: { enabled: false },
                    fontSize: 13,
                    lineNumbers: "on",
                    wordWrap: "on",
                    scrollBeyondLastLine: false,
                    padding: { top: 10, bottom: 10 },
                    renderLineHighlight: "none",
                    overviewRulerBorder: false,
                    hideCursorInOverviewRuler: true,
                    automaticLayout: true,
                    fontFamily: "'SF Mono','JetBrains Mono','Fira Code',Consolas,monospace",
                    lineHeight: 20,
                    cursorBlinking: "smooth",
                    smoothScrolling: true,
                  }}
                />
              )}
            </div>
          </>
        ) : (
          <div className="flex flex-col items-center justify-center flex-1 text-gray-400">
            <FileText className="w-10 h-10 mb-2 text-gray-300" />
            <p className="text-sm font-medium text-gray-500">No file selected</p>
            <p className="text-[11px] mt-1 text-gray-400">Choose from the list above</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── File List Component ─────────────────────────────────

function FileList({
  files,
  onSelect,
  selectedPath,
  tokenCounts,
}: {
  files: Array<{ label: string; path: string; icon: React.ElementType; color: string }>;
  onSelect: (path: string) => void;
  selectedPath: string | null;
  tokenCounts?: Record<string, number>;
}) {
  return (
    <div className="space-y-0.5">
      <p className="px-3 pt-1 pb-1 text-[10px] font-semibold text-gray-400 uppercase tracking-widest">
        Workspace
      </p>
      {files.map((f) => {
        const Icon = f.icon;
        const active = selectedPath === f.path;
        const count = tokenCounts?.[f.path];
        return (
          <button
            key={f.path}
            onClick={() => onSelect(f.path)}
            className={`w-full flex items-center gap-2 px-3 py-1.5 text-[12px] rounded-lg transition-all text-left relative ${
              active
                ? "bg-white/70 text-gray-800 font-medium shadow-sm"
                : "text-gray-500 hover:bg-white/40"
            }`}
          >
            {active && (
              <div
                className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-r-full"
                style={{ background: f.color }}
              />
            )}
            <Icon className="w-3.5 h-3.5 shrink-0" style={{ color: f.color }} />
            <span className="truncate flex-1">{f.label}</span>
            {count !== undefined && count > 0 && (
              <span className="text-[10px] text-gray-400 shrink-0">{count}t</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ── MCP Server List ─────────────────────────────────────

function McpServerList({
  servers,
}: {
  servers: Array<{ key: string; name: string; url: string; transport: string }>;
}) {
  return (
    <div className="space-y-0.5">
      <p className="px-3 pt-1 pb-1 text-[10px] font-semibold text-gray-400 uppercase tracking-widest">
        Enabled MCP Servers
      </p>
      {servers.length === 0 && (
        <p className="px-3 py-2 text-[11px] text-gray-400">
          No MCP servers enabled. Add one in backend/config.json.
        </p>
      )}
      {servers.map((s) => (
        <div
          key={s.key}
          className="w-full flex items-start gap-2 px-3 py-2 text-[12px] rounded-lg text-left text-gray-500"
        >
          <Server className="w-3.5 h-3.5 shrink-0 text-[#0ea5e9] mt-0.5" />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-gray-700 mb-0.5">{s.name}</div>
            <div className="text-[10px] text-gray-400 leading-relaxed truncate">
              {s.transport} · {s.url}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Skills File List ────────────────────────────────────

function SkillsFileList({
  skills,
  activeSkills,
  onSelect,
  selectedPath,
  onNavigateToConfig,
}: {
  skills: Array<{ name: string; path: string; description: string }>;
  activeSkills: Array<{ name: string; description: string }>;
  onSelect: (path: string) => void;
  selectedPath: string | null;
  onNavigateToConfig: () => void;
}) {
  return (
    <div className="space-y-3">
      {/* Navigate to Skills Config button */}
      <button
        onClick={onNavigateToConfig}
        className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-[12px] font-medium text-white bg-[#002fa7] hover:bg-[#001f7a] transition-all active:scale-95"
      >
        <Settings2 className="w-3.5 h-3.5" />
        打开 Skills 配置页面
      </button>

      {/* Active Skills Section */}
      {activeSkills.length > 0 && (
        <div className="space-y-0.5">
          <p className="px-3 pt-1 pb-1 text-[10px] font-semibold text-green-600 uppercase tracking-widest">
            ✓ Active Skills
          </p>
          {activeSkills.map((s) => {
            const skillPath = skills.find(sk => sk.name === s.name)?.path;
            const active = selectedPath === skillPath;
            return (
              <button
                key={s.name}
                onClick={() => skillPath && onSelect(skillPath)}
                className={`w-full flex items-start gap-2 px-3 py-2 text-[12px] rounded-lg transition-all text-left relative ${
                  active
                    ? "bg-white/70 text-gray-800 font-medium shadow-sm"
                    : "text-gray-500 hover:bg-white/40"
                }`}
              >
                {active && (
                  <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-6 bg-green-500 rounded-r-full" />
                )}
                <Zap className="w-3.5 h-3.5 shrink-0 text-green-500 mt-0.5" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-gray-700 mb-0.5">{s.name}</div>
                  {s.description && (
                    <div className="text-[10px] text-gray-400 leading-relaxed line-clamp-2">
                      {s.description}
                    </div>
                  )}
                </div>
              </button>
            );
          })}
        </div>
      )}

      {/* Available Skills Section */}
      <div className="space-y-0.5">
        <p className="px-3 pt-1 pb-1 text-[10px] font-semibold text-gray-400 uppercase tracking-widest">
          Available Skills
        </p>
        {skills.length === 0 && (
          <p className="px-3 py-2 text-[11px] text-gray-400">No skills found</p>
        )}
        {skills.map((s) => {
          const active = selectedPath === s.path;
          const isActive = activeSkills.some(as => as.name === s.name);
          if (isActive) return null; // Don't show in available if already active

          return (
            <button
              key={s.name}
              onClick={() => onSelect(s.path)}
              className={`w-full flex items-start gap-2 px-3 py-2 text-[12px] rounded-lg transition-all text-left relative ${
                active
                  ? "bg-white/70 text-gray-800 font-medium shadow-sm"
                  : "text-gray-500 hover:bg-white/40"
              }`}
            >
              {active && (
                <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-6 bg-[#f59e0b] rounded-r-full" />
              )}
              <Zap className="w-3.5 h-3.5 shrink-0 text-[#f59e0b] mt-0.5" />
              <div className="min-w-0 flex-1">
                <div className="font-medium text-gray-700 mb-0.5">{s.name}</div>
                {s.description && (
                  <div className="text-[10px] text-gray-400 leading-relaxed line-clamp-2">
                    {s.description}
                  </div>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
