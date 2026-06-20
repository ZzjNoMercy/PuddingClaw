"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import dynamic from "next/dynamic";
import { readFile, saveFile, getFileTokenCounts } from "@/lib/api";
import {
  Save,
  FileText,
  Loader2,
  CheckCircle2,
  AlertCircle,
  Brain,
  Sparkles,
} from "lucide-react";
import "@/lib/monaco-config";

const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full text-gray-400 text-sm">
      <Loader2 className="w-4 h-4 animate-spin mr-2" />Loading editor...
    </div>
  ),
});

const MEMORY_FILES = [
  { label: "MEMORY.md", path: "memory/MEMORY.md", icon: Brain, color: "#7c3aed" },
  { label: "SOUL.md", path: "workspace/SOUL.md", icon: Sparkles, color: "#f59e0b" },
  { label: "IDENTITY.md", path: "workspace/IDENTITY.md", icon: FileText, color: "#6b7280" },
  { label: "USER.md", path: "workspace/USER.md", icon: FileText, color: "#6b7280" },
  { label: "AGENTS.md", path: "workspace/AGENTS.md", icon: FileText, color: "#10b981" },
  { label: "SKILLS_SNAPSHOT.md", path: "SKILLS_SNAPSHOT.md", icon: Sparkles, color: "#f59e0b" },
];

export default function MemoryEditor() {
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<"idle" | "saved" | "error">("idle");
  const [tokenCounts, setTokenCounts] = useState<Record<string, number>>({});
  const editorRef = useRef<unknown>(null);

  const isDirty = content !== originalContent;
  const fileName = selectedPath?.split("/").pop() || "";
  const fileExt = fileName.split(".").pop() || "md";
  const language = fileExt === "md" ? "markdown" : fileExt === "json" ? "json" : "markdown";

  useEffect(() => {
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
  }, []);

  useEffect(() => {
    if (!selectedPath) {
      setContent("");
      setOriginalContent("");
      return;
    }
    setLoading(true);
    setSaveStatus("idle");
    readFile(selectedPath)
      .then((t) => {
        setContent(t);
        setOriginalContent(t);
      })
      .catch(() => {
        setContent("# Error loading file");
        setOriginalContent("");
      })
      .finally(() => setLoading(false));
  }, [selectedPath]);

  const handleSave = useCallback(async () => {
    if (!selectedPath || saving) return;
    setSaving(true);
    setSaveStatus("idle");
    try {
      await saveFile(selectedPath, content);
      setOriginalContent(content);
      setSaveStatus("saved");
      setTimeout(() => setSaveStatus("idle"), 2000);
      // Refresh token counts after save
      const data = await getFileTokenCounts(MEMORY_FILES.map((f) => f.path));
      const counts: Record<string, number> = {};
      for (const f of data.files) {
        counts[f.path] = f.tokens;
      }
      setTokenCounts(counts);
    } catch {
      setSaveStatus("error");
    } finally {
      setSaving(false);
    }
  }, [selectedPath, content, saving]);

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

  return (
    <div className="flex flex-col h-full bg-white/60 backdrop-blur-xl rounded-2xl border border-black/[0.06] shadow-sm overflow-hidden">
      <div className="flex-1 flex min-h-0">
        {/* File list */}
        <div className="w-52 shrink-0 border-r border-black/[0.06] p-2 overflow-y-auto">
          <p className="px-3 pt-1 pb-1 text-[10px] font-semibold text-gray-400 uppercase tracking-widest">
            Workspace
          </p>
          <div className="space-y-0.5">
            {MEMORY_FILES.map((f) => {
              const Icon = f.icon;
              const active = selectedPath === f.path;
              const count = tokenCounts[f.path];
              return (
                <button
                  key={f.path}
                  onClick={() => setSelectedPath(f.path)}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-[12px] rounded-lg transition-all text-left relative ${
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
        </div>

        {/* Editor */}
        <div className="flex-1 flex flex-col min-w-0">
          {selectedPath ? (
            <>
              <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b border-black/[0.06] bg-white/50">
                <div className="flex items-center gap-2 min-w-0">
                  <FileText className="w-3.5 h-3.5 text-[#ff6723] shrink-0" />
                  <div className="text-[12px] font-semibold text-gray-700 truncate">
                    {fileName}
                    {isDirty && <span className="ml-1.5 w-1.5 h-1.5 bg-amber-400 rounded-full inline-block align-middle" />}
                  </div>
                </div>
                <div className="flex items-center gap-1.5">
                  {saveStatus === "saved" && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" />}
                  {saveStatus === "error" && <AlertCircle className="w-3.5 h-3.5 text-red-500" />}
                  <button
                    onClick={handleSave}
                    disabled={saving || !isDirty}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded-md text-[11px] font-medium text-white bg-[#ff6723] disabled:opacity-25 hover:bg-[#e55a1b] transition-all active:scale-95"
                    title="Save (Ctrl+S)"
                  >
                    {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
                    Save
                  </button>
                </div>
              </div>
              <div className="flex-1 min-h-0">
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
              <Brain className="w-10 h-10 mb-2 text-gray-300" />
              <p className="text-sm font-medium text-gray-500">No memory file selected</p>
              <p className="text-[11px] mt-1 text-gray-400">Choose a file from the list to edit</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
