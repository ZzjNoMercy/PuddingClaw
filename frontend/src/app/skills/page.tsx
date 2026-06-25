"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import dynamic from "next/dynamic";
import { usePathname, useRouter } from "next/navigation";
import "@/lib/monaco-config";
import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import FileTree from "@/components/skills/FileTree";
import { useApp } from "@/lib/store";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Zap,
  Search,
  Plus,
  Loader2,
  Save,
  CheckCircle2,
  XCircle,
  Trash2,
  MoreHorizontal,
  FileText,
  FolderOpen,
  Eye,
  EyeOff,
  X,
  ChevronRight,
  ChevronLeft,
  File,
  Clock,
  HardDrive,
  Upload,
  Sparkles,
  Server,
} from "lucide-react";
import {
  listSkills,
  getSkill,
  createSkill,
  deleteSkill,
  renameSkill,
  saveSkillContent,
  getSkillFile,
  getSkillTree,
  type SkillInfo,
  type SkillDetail,
  type SkillFileInfo,
  type FileTreeNode,
} from "@/lib/skillsApi";

const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => (
    <div className="flex-1 flex items-center justify-center">
      <Loader2 className="w-5 h-5 animate-spin text-gray-300" />
    </div>
  ),
});

// ── Main Page ────────────────────────────────────────────
export default function SkillsPage() {
  const {
    sidebarOpen,
    toggleSidebar,
    sidebarWidth,
    mcpServers,
    loadMcpServers,
    triggerSkillCreator,
    setPendingInput,
  } = useApp();
  const [extensionView, setExtensionView] = useState<"mcp" | "skills">("skills");
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [skillDetail, setSkillDetail] = useState<SkillDetail | null>(null);
  const [skillTree, setSkillTree] = useState<FileTreeNode[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [editorContent, setEditorContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [activeFile, setActiveFile] = useState("SKILL.md");
  const [showPreview, setShowPreview] = useState(true);
  const [showNewModal, setShowNewModal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [contextMenu, setContextMenu] = useState<{ skillName: string; x: number; y: number } | null>(null);
  const contextMenuRef = useRef<HTMLDivElement>(null);
  const [showImportModal, setShowImportModal] = useState(false);
  const [importing, setImporting] = useState(false);
  const [showRenameModal, setShowRenameModal] = useState(false);
  const [renamingSkill, setRenamingSkill] = useState<string | null>(null);
  const pathname = usePathname();
  const router = useRouter();

  // Dirty flag
  const isDirty = editorContent !== originalContent;

  // ── Toast helper ─────────────────────────────────────
  const showToast = useCallback((type: "success" | "error", message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 3000);
  }, []);

  // ── Load skills list ─────────────────────────────────
  const loadSkills = useCallback(async () => {
    try {
      const data = await listSkills();
      setSkills(data);
    } catch {
      showToast("error", "Failed to load skills");
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    loadSkills();
  }, [loadSkills]);

  useEffect(() => {
    if (extensionView === "mcp") loadMcpServers();
  }, [extensionView, loadMcpServers]);

  // ── Load skill detail ────────────────────────────────
  const loadSkillDetail = useCallback(
    async (name: string) => {
      setDetailLoading(true);
      try {
        const [detail, tree] = await Promise.all([
          getSkill(name),
          getSkillTree(name)
        ]);
        setSkillDetail(detail);
        setSkillTree(tree.files);
        setEditorContent(detail.content);
        setOriginalContent(detail.content);
        setActiveFile("SKILL.md");
      } catch {
        showToast("error", `Failed to load skill: ${name}`);
      } finally {
        setDetailLoading(false);
      }
    },
    [showToast]
  );

  const handleSelectSkill = useCallback(
    (name: string) => {
      if (isDirty) {
        if (!window.confirm("当前文件有未保存的更改，确定要切换吗？")) return;
      }
      setSelectedSkill(name);
      loadSkillDetail(name);
    },
    [isDirty, loadSkillDetail]
  );

  // ── Load file content when activeFile changes ───────────
  useEffect(() => {
    if (!selectedSkill || !activeFile) return;

    const loadFileContent = async () => {
      if (isDirty) {
        if (!window.confirm("当前文件有未保存的更改，确定要切换吗？")) {
          // Revert activeFile to previous value
          return;
        }
      }

      setDetailLoading(true);
      try {
        const fileData = await getSkillFile(selectedSkill, activeFile);
        setEditorContent(fileData.content);
        setOriginalContent(fileData.content);
      } catch (err) {
        showToast("error", err instanceof Error ? err.message : "加载文件失败");
      } finally {
        setDetailLoading(false);
      }
    };

    loadFileContent();
  }, [selectedSkill, activeFile]); // Note: intentionally not including isDirty to avoid infinite loop

  // ── Save file ────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!selectedSkill || !isDirty) return;
    setSaving(true);
    try {
      await saveSkillContent(selectedSkill, activeFile, editorContent);
      setOriginalContent(editorContent);
      showToast("success", "文件已保存");
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }, [selectedSkill, activeFile, editorContent, isDirty, showToast]);

  // ── Keyboard shortcut: Cmd/Ctrl+S ───────────────────
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        handleSave();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [handleSave]);

  // ── Create skill ─────────────────────────────────────
  const handleCreateSkill = useCallback(
    async (name: string, description: string) => {
      try {
        await createSkill(name, description);
        showToast("success", `Skill "${name}" 创建成功`);
        setShowNewModal(false);
        await loadSkills();
        setSelectedSkill(name);
        loadSkillDetail(name);
      } catch (err) {
        showToast("error", err instanceof Error ? err.message : "创建失败");
      }
    },
    [showToast, loadSkills, loadSkillDetail]
  );

  // ── Import skill from ZIP ────────────────────────────
  const handleImportSkill = useCallback(
    async (file: File) => {
      setImporting(true);
      try {
        const formData = new FormData();
        formData.append("file", file);

        const response = await fetch("/api/skills/import", {
          method: "POST",
          body: formData,
        });

        if (!response.ok) {
          const error = await response.json();
          throw new Error(error.detail || "导入失败");
        }

        const result = await response.json();
        showToast("success", `Skill "${result.skill_name}" 导入成功`);
        loadSkills();
        setShowImportModal(false);
      } catch (err) {
        showToast("error", err instanceof Error ? err.message : "导入失败");
      } finally {
        setImporting(false);
      }
    },
    [loadSkills, showToast]
  );

  // ── Create or try-out a skill in chat ────────────────
  const handleNavigateToCreate = useCallback(() => {
    if (selectedSkill) {
      // When a skill is selected, prefill its slash command so the user can
      // try it out in a new chat. The session is created lazily on send.
      setPendingInput(`/${selectedSkill} `);
      if (pathname !== "/") {
        router.push("/");
      }
    } else {
      // In the default list view, prefill the skill-creator prompt.
      triggerSkillCreator();
      if (pathname !== "/") {
        router.push("/?trigger=skill-creator&action=create");
      }
    }
  }, [pathname, router, selectedSkill, setPendingInput, triggerSkillCreator]);

  // ── Delete skill ─────────────────────────────────────
  const handleDeleteSkill = useCallback(
    async (name: string) => {
      if (!window.confirm(`确定删除 Skill "${name}" 吗？此操作不可撤销。`)) return;
      try {
        await deleteSkill(name);
        showToast("success", `Skill "${name}" 已删除`);
        if (selectedSkill === name) {
          setSelectedSkill(null);
          setSkillDetail(null);
          setEditorContent("");
          setOriginalContent("");
        }
        await loadSkills();
      } catch (err) {
        showToast("error", err instanceof Error ? err.message : "删除失败");
      }
      setContextMenu(null);
    },
    [selectedSkill, showToast, loadSkills]
  );

  // ── Rename skill ─────────────────────────────────────
  const handleRenameSkill = useCallback(
    async (oldName: string, newName: string) => {
      if (!newName.trim()) {
        showToast("error", "Skill 名称不能为空");
        return;
      }

      try {
        await renameSkill(oldName, newName.trim());
        showToast("success", `Skill 已重命名为 "${newName}"`);

        // Update selected skill if it was renamed
        if (selectedSkill === oldName) {
          setSelectedSkill(newName.trim());
        }

        await loadSkills();
        setShowRenameModal(false);
        setRenamingSkill(null);
      } catch (err) {
        showToast("error", err instanceof Error ? err.message : "重命名失败");
      }
    },
    [selectedSkill, showToast, loadSkills]
  );

  // ── Context menu close on outside click ──────────────
  useEffect(() => {
    if (!contextMenu) return;
    const handler = (e: MouseEvent) => {
      if (contextMenuRef.current && !contextMenuRef.current.contains(e.target as Node)) {
        setContextMenu(null);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [contextMenu]);

  // ── Filtered skills ──────────────────────────────────
  const filteredSkills = skills.filter(
    (s) =>
      s.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      s.description.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const handleExtensionViewChange = useCallback(
    (view: "mcp" | "skills") => {
      if (view === extensionView) return;
      if (isDirty && !window.confirm("当前文件有未保存的更改，确定要切换吗？")) return;
      setExtensionView(view);
    },
    [extensionView, isDirty]
  );

  // ── Loading state ────────────────────────────────────
  if (loading) {
    return (
      <div className="h-screen flex flex-col app-bg">
        <Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles />
        <div className="flex-1 flex overflow-hidden p-2 pt-0">
          <div
            className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden"
            style={{ width: sidebarOpen ? sidebarWidth : 0 }}
          >
            <div style={{ width: sidebarWidth, minWidth: 200 }} className="h-full">
              <Sidebar />
            </div>
          </div>
          <div className="flex-1 flex items-center justify-center">
            <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col app-bg">
      <Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles />
      <div className="flex-1 flex overflow-hidden p-2 pt-0">
        <div
          className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden"
          style={{ width: sidebarOpen ? sidebarWidth : 0 }}
        >
          <div style={{ width: sidebarWidth, minWidth: 200 }} className="h-full">
            <Sidebar />
          </div>
        </div>

        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="flex h-11 shrink-0 items-center border-b border-black/[0.12] bg-white px-4">
        <div className="flex items-center gap-1 rounded-lg bg-gray-100 p-1" role="tablist" aria-label="扩展类型">
          <button
            type="button"
            role="tab"
            aria-selected={extensionView === "skills"}
            onClick={() => handleExtensionViewChange("skills")}
            className={`rounded-md px-3 py-1 text-[12px] font-medium transition-colors ${
              extensionView === "skills"
                ? "bg-white text-gray-900 shadow-sm"
                : "text-gray-500 hover:text-gray-800"
            }`}
          >
            技能
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={extensionView === "mcp"}
            onClick={() => handleExtensionViewChange("mcp")}
            className={`rounded-md px-3 py-1 text-[12px] font-medium transition-colors ${
              extensionView === "mcp"
                ? "bg-white text-gray-900 shadow-sm"
                : "text-gray-500 hover:text-gray-800"
            }`}
          >
            MCP
          </button>
        </div>
      </div>

      {extensionView === "mcp" ? (
        <McpServerCatalog servers={mcpServers} />
      ) : (
      <div className="flex-1 flex overflow-hidden">
        {/* ── Skills catalog / editor navigation ─────────── */}
        <div className={`${
          selectedSkill
            ? "w-[240px] shrink-0 border-r border-black/[0.06]"
            : "flex-1"
        } glass-panel flex min-w-0 flex-col`}>
          {/* Header */}
          <div className={selectedSkill ? "border-b border-black/[0.06] p-3" : "mx-auto w-full max-w-4xl px-6 pb-4 pt-10"}>
            {!selectedSkill && (
              <div className="mb-6">
                <h1 className="text-2xl font-semibold text-gray-900">技能</h1>
                <p className="mt-1 text-[13px] text-gray-500">通过任务专用技能扩展 PuddingClaw 的能力</p>
              </div>
            )}
            <div className="flex items-center justify-between mb-2.5">
              <div className="flex items-center gap-2">
                {selectedSkill && (
                  <button
                    type="button"
                    onClick={() => setSelectedSkill(null)}
                    className="flex h-7 w-7 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-black/[0.04] hover:text-gray-700"
                    title="返回技能列表"
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </button>
                )}
                <Zap className="w-4 h-4 text-[#002fa7]" />
                <span className="text-[13px] font-semibold text-gray-700">{selectedSkill ? "Skills" : "已安装"}</span>
                <span className="text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded-full">
                  {skills.length}
                </span>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setShowImportModal(true)}
                  className="w-7 h-7 flex items-center justify-center rounded-lg bg-blue-500/10 text-blue-600 hover:bg-blue-500/20 transition-colors"
                  title="导入 Skill"
                >
                  <Upload className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={handleNavigateToCreate}
                  className="w-7 h-7 flex items-center justify-center rounded-lg bg-purple-500/10 text-purple-600 hover:bg-purple-500/20 transition-colors"
                  title={selectedSkill ? `试用 ${selectedSkill}` : "创建 Skill"}
                >
                  <Sparkles className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={() => setShowNewModal(true)}
                  className="w-7 h-7 flex items-center justify-center rounded-lg bg-[#002fa7]/10 text-[#001f7a] hover:bg-[#002fa7]/20 transition-colors"
                  title="新建 Skill"
                >
                  <Plus className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
            {/* Search */}
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
              <input
                type="text"
                placeholder="搜索技能"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className={`w-full border border-black/[0.06] bg-white outline-none transition-all placeholder:text-gray-400 focus:border-[#002fa7]/40 focus:ring-1 focus:ring-[#002fa7]/10 ${
                  selectedSkill
                    ? "rounded-lg py-1.5 pl-8 pr-3 text-[12px]"
                    : "rounded-xl py-3 pl-10 pr-4 text-[13px] shadow-sm"
                }`}
              />
            </div>
          </div>

          {/* List */}
          <div className={selectedSkill ? "flex-1 space-y-0.5 overflow-y-auto p-2" : "mx-auto w-full max-w-4xl flex-1 space-y-1 overflow-y-auto px-6 pb-10"}>
            {filteredSkills.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-gray-400">
                <FolderOpen className="w-8 h-8 mb-2 opacity-40" />
                <span className="text-[12px]">
                  {searchQuery ? "无匹配结果" : "暂无 Skills"}
                </span>
              </div>
            ) : (
              filteredSkills.map((skill) => (
                <div
                  key={skill.name}
                  onClick={() => handleSelectSkill(skill.name)}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    setContextMenu({ skillName: skill.name, x: e.clientX, y: e.clientY });
                  }}
                  className={`group flex cursor-pointer items-center transition-all ${
                    selectedSkill ? "gap-2.5 rounded-lg px-2.5 py-2" : "gap-3 rounded-lg px-4 py-3"
                  } ${
                    selectedSkill === skill.name
                      ? "bg-[#002fa7]/10 border border-[#002fa7]/20"
                      : "hover:bg-white/70 border border-transparent"
                  }`}
                >
                  <div
                    className={`${selectedSkill ? "h-7 w-7" : "h-10 w-10"} flex shrink-0 items-center justify-center rounded-lg ${
                      selectedSkill === skill.name
                        ? "bg-[#002fa7] text-white"
                        : "bg-gray-100 text-gray-400 group-hover:bg-[#002fa7]/10 group-hover:text-[#002fa7]"
                    } transition-colors`}
                  >
                    <Zap className={selectedSkill ? "h-3.5 w-3.5" : "h-5 w-5"} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className={`${selectedSkill ? "text-[12px]" : "text-[14px]"} truncate font-medium text-gray-700`}>
                      {skill.name}
                    </div>
                    {!selectedSkill && (
                      <div className="mt-0.5 text-[12px] text-gray-500 line-clamp-2 leading-relaxed">
                        {skill.description || "No description"}
                      </div>
                    )}
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setContextMenu({
                        skillName: skill.name,
                        x: e.currentTarget.getBoundingClientRect().right,
                        y: e.currentTarget.getBoundingClientRect().top,
                      });
                    }}
                    className="opacity-0 group-hover:opacity-100 w-6 h-6 flex items-center justify-center rounded-md hover:bg-black/[0.06] transition-all shrink-0"
                  >
                    <MoreHorizontal className="w-3.5 h-3.5 text-gray-400" />
                  </button>
                </div>
              ))
            )}

          </div>
        </div>

        {/* ── Center: Editor / Preview ─────────────────────────────── */}
        {selectedSkill && (
        <div className="flex-1 flex flex-col min-w-0">
          {detailLoading ? (
            <div className="flex-1 flex items-center justify-center">
              <Loader2 className="w-5 h-5 animate-spin text-gray-300" />
            </div>
          ) : (
            <>
              {/* Toolbar */}
              <div className="h-10 flex items-center justify-between px-3 border-b border-black/[0.06] bg-white/40 shrink-0">
                <div className="flex items-center gap-2">
                  {/* Current file indicator */}
                  <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-[#002fa7]/10 text-[#001f7a] border border-[#002fa7]/20">
                    <FileText className="w-3 h-3" />
                    <span className="text-[11px] font-medium">{activeFile}</span>
                  </div>
                  {/* Dirty indicator */}
                  {isDirty && (
                    <span className="w-2 h-2 rounded-full bg-[#4a6de5] animate-pulse" title="未保存的更改" />
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setShowPreview((v) => !v)}
                    className={`w-7 h-7 flex items-center justify-center rounded-lg transition-all ${
                      showPreview
                        ? "bg-[#002fa7]/10 text-[#001f7a]"
                        : "text-gray-400 hover:text-gray-600 hover:bg-black/[0.04]"
                    }`}
                    title={showPreview ? "切换为编辑" : "切换为预览"}
                  >
                    {showPreview ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
                  </button>
                  <button
                    onClick={handleSave}
                    disabled={saving || !isDirty}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-medium rounded-lg bg-[#002fa7] text-white hover:bg-[#001f7a] transition-colors disabled:opacity-40 disabled:cursor-not-allowed shadow-sm"
                  >
                    {saving ? (
                      <Loader2 className="w-3 h-3 animate-spin" />
                    ) : (
                      <Save className="w-3 h-3" />
                    )}
                    保存
                  </button>
                </div>
              </div>

              {/* Editor or Markdown Preview in the center */}
              <div className="flex-1 overflow-hidden">
                {showPreview ? (
                  <div className="h-full overflow-y-auto p-6 bg-white/30">
                    <div className="max-w-3xl mx-auto markdown-content">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {editorContent}
                      </ReactMarkdown>
                    </div>
                  </div>
                ) : (
                  <div className="h-full">
                    <MonacoEditor
                      height="100%"
                      language="markdown"
                      theme="vs"
                      value={editorContent}
                      onChange={(value) => setEditorContent(value || "")}
                      options={{
                        fontSize: 13,
                        fontFamily: "'SF Mono', 'JetBrains Mono', 'Fira Code', Consolas, monospace",
                        lineHeight: 22,
                        minimap: { enabled: false },
                        wordWrap: "on",
                        padding: { top: 12, bottom: 12 },
                        scrollBeyondLastLine: false,
                        renderLineHighlight: "gutter",
                        overviewRulerBorder: false,
                        hideCursorInOverviewRuler: true,
                        lineNumbers: "on",
                        glyphMargin: false,
                        folding: true,
                        lineDecorationsWidth: 8,
                        contextmenu: false,
                      }}
                    />
                  </div>
                )}
              </div>
            </>
          )}
        </div>
        )}

        {/* ── Right: Skill meta + Description + Files ───────────── */}
        {selectedSkill && (
          <div className="w-[260px] shrink-0 border-l border-black/[0.06] flex flex-col bg-white/30">
            <div className="p-3 border-b border-black/[0.06]">
              {/* DESCRIPTION */}
              <div className="mb-3">
                <div className="text-[10px] font-medium text-gray-400 uppercase tracking-wider mb-1.5 px-2">
                  Description
                </div>
                <div className="max-h-[120px] overflow-y-auto px-2 py-1.5 text-[11px] text-gray-600 leading-relaxed bg-black/[0.02] rounded-md">
                  {skillDetail?.description || "No description"}
                </div>
              </div>

              {/* FILES */}
              {skillTree.length > 0 && (
                <div>
                  <div className="text-[10px] font-medium text-gray-400 uppercase tracking-wider mb-1.5 px-2">
                    Files
                  </div>
                  <FileTree
                    nodes={skillTree}
                    activeFile={activeFile}
                    onFileSelect={setActiveFile}
                  />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      )}
        </main>
      </div>

      {/* ── Context Menu ──────────────────────────────────── */}
      {contextMenu && (
        <div
          ref={contextMenuRef}
          className="fixed z-50 bg-white/95 backdrop-blur-xl rounded-lg shadow-lg border border-black/[0.06] py-1 w-36 animate-fade-in-scale"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          <button
            onClick={() => {
              setRenamingSkill(contextMenu.skillName);
              setShowRenameModal(true);
              setContextMenu(null);
            }}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] text-gray-700 hover:bg-gray-50 transition-colors"
          >
            <FileText className="w-3.5 h-3.5" />
            重命名 Skill
          </button>
          <button
            onClick={() => handleDeleteSkill(contextMenu.skillName)}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] text-red-500 hover:bg-red-50 transition-colors"
          >
            <Trash2 className="w-3.5 h-3.5" />
            删除 Skill
          </button>
        </div>
      )}

      {/* ── New Skill Modal ───────────────────────────────── */}
      {showNewModal && (
        <NewSkillModal
          onClose={() => setShowNewModal(false)}
          onCreate={handleCreateSkill}
        />
      )}

      {/* ── Import Skill Modal ────────────────────────────── */}
      {showImportModal && (
        <ImportSkillModal
          onClose={() => setShowImportModal(false)}
          onImport={handleImportSkill}
          importing={importing}
        />
      )}

      {/* ── Rename Skill Modal ────────────────────────────── */}
      {showRenameModal && renamingSkill && (
        <RenameSkillModal
          currentName={renamingSkill}
          onClose={() => {
            setShowRenameModal(false);
            setRenamingSkill(null);
          }}
          onRename={handleRenameSkill}
        />
      )}

      {/* ── Toast ─────────────────────────────────────────── */}
      {toast && (
        <div
          className={`fixed bottom-6 right-6 flex items-center gap-2 px-4 py-2.5 rounded-xl text-[13px] font-medium shadow-lg animate-fade-in z-50 ${
            toast.type === "success"
              ? "bg-emerald-500 text-white"
              : "bg-red-500 text-white"
          }`}
        >
          {toast.type === "success" ? (
            <CheckCircle2 className="w-4 h-4" />
          ) : (
            <XCircle className="w-4 h-4" />
          )}
          {toast.message}
        </div>
      )}
    </div>
  );
}

function McpServerCatalog({
  servers,
}: {
  servers: Array<{ key: string; name: string; url: string; transport: string }>;
}) {
  return (
    <div className="flex-1 overflow-y-auto bg-white/30">
      <div className="mx-auto w-full max-w-4xl px-6 pb-10 pt-10">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold text-gray-900">MCP</h1>
          <p className="mt-1 text-[13px] text-gray-500">连接外部服务，为对话提供工具与数据能力</p>
        </div>

        <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-gray-700">
          <Server className="h-4 w-4 text-sky-500" />
          已启用
          <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] font-normal text-gray-500">
            {servers.length}
          </span>
        </div>

        {servers.length === 0 ? (
          <div className="rounded-lg border border-dashed border-black/[0.08] px-4 py-8 text-center text-[12px] text-gray-400">
            暂无已配置 MCP Server
          </div>
        ) : (
          <div className="space-y-1">
            {servers.map((server) => (
              <div
                key={server.key}
                className="flex items-center gap-3 rounded-lg border border-transparent px-4 py-3 transition-colors hover:border-black/[0.05] hover:bg-white/70"
              >
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-600">
                  <Server className="h-5 w-5" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[14px] font-medium text-gray-800">{server.name}</p>
                  <p className="mt-0.5 truncate text-[12px] text-gray-400">
                    {server.transport} · {server.url}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1 text-[11px] text-emerald-600">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  已启用
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Empty Editor Placeholder ─────────────────────────────
function EmptyEditor() {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center">
        <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-[#4a6de5] to-[#002fa7] flex items-center justify-center mx-auto mb-4 shadow-lg shadow-[#4a6de5]/15">
          <Zap className="w-8 h-8 text-white" />
        </div>
        <h2 className="text-[15px] font-semibold text-gray-700 mb-1">
          选择一个 Skill 开始编辑
        </h2>
        <p className="text-[12px] text-gray-400 max-w-[240px] mx-auto leading-relaxed">
          从左侧列表选择已有 Skill，或点击 + 按钮创建新的 Skill
        </p>
        <div className="flex items-center justify-center gap-4 mt-4 text-[11px] text-gray-400">
          <span className="flex items-center gap-1">
            <ChevronRight className="w-3 h-3" /> 浏览
          </span>
          <span className="flex items-center gap-1">
            <FileText className="w-3 h-3" /> 编辑
          </span>
          <span className="flex items-center gap-1">
            <Eye className="w-3 h-3" /> 预览
          </span>
        </div>
      </div>
    </div>
  );
}

// ── New Skill Modal ──────────────────────────────────────
function NewSkillModal({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (name: string, description: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleSubmit = async () => {
    if (!name.trim()) return;
    setCreating(true);
    try {
      await onCreate(name.trim(), description.trim());
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/20 backdrop-blur-sm"
        onClick={onClose}
      />
      {/* Modal */}
      <div className="relative bg-white/95 backdrop-blur-xl rounded-2xl shadow-2xl border border-black/[0.06] w-[400px] p-5 animate-fade-in-scale">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-[#002fa7]/10 flex items-center justify-center">
              <Plus className="w-4 h-4 text-[#001f7a]" />
            </div>
            <h3 className="text-[14px] font-semibold text-gray-800">新建 Skill</h3>
          </div>
          <button
            onClick={onClose}
            className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-black/[0.06] transition-colors"
          >
            <X className="w-4 h-4 text-gray-400" />
          </button>
        </div>

        <div className="space-y-3">
          <div>
            <label className="block text-[11px] font-medium text-gray-500 mb-1.5">
              Skill 名称
            </label>
            <input
              ref={inputRef}
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
              className="form-input"
              placeholder="my-awesome-skill"
            />
          </div>
          <div>
            <label className="block text-[11px] font-medium text-gray-500 mb-1.5">
              描述
            </label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
              className="form-input"
              placeholder="这个 Skill 的用途..."
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 mt-5">
          <button
            onClick={onClose}
            className="px-4 py-2 text-[12px] font-medium text-gray-500 hover:bg-black/[0.04] rounded-lg transition-colors"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={creating || !name.trim()}
            className="flex items-center gap-1.5 px-4 py-2 text-[12px] font-medium text-white bg-[#002fa7] hover:bg-[#001f7a] rounded-lg transition-colors disabled:opacity-50 shadow-sm shadow-[#002fa7]/15"
          >
            {creating ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Plus className="w-3.5 h-3.5" />
            )}
            创建
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Import Skill Modal ──────────────────────────────────
type UploadMode = "zip" | "skill" | "folder";

function ImportSkillModal({
  onClose,
  onImport,
  importing,
}: {
  onClose: () => void;
  onImport: (file: File) => Promise<void>;
  importing: boolean;
}) {
  const [uploadMode, setUploadMode] = useState<UploadMode>("zip");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [selectedFiles, setSelectedFiles] = useState<FileList | null>(null);
  const [skillName, setSkillName] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === "dragenter" || e.type === "dragover") {
      setDragActive(true);
    } else if (e.type === "dragleave") {
      setDragActive(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    if (uploadMode === "folder") {
      // Folder mode: accept multiple files
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        setSelectedFiles(e.dataTransfer.files);

        // Auto-extract folder name from webkitRelativePath
        const firstFile = e.dataTransfer.files[0];
        if (firstFile.webkitRelativePath) {
          const folderName = firstFile.webkitRelativePath.split("/")[0];
          setSkillName(folderName);
        }
      }
    } else {
      // Single file mode
      if (e.dataTransfer.files && e.dataTransfer.files[0]) {
        const file = e.dataTransfer.files[0];
        const expectedExt = uploadMode === "zip" ? ".zip" : ".skill";
        if (file.name.endsWith(expectedExt)) {
          setSelectedFile(file);
        }
      }
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (uploadMode === "folder") {
      if (e.target.files && e.target.files.length > 0) {
        setSelectedFiles(e.target.files);

        // Auto-extract folder name from webkitRelativePath
        const firstFile = e.target.files[0];
        if (firstFile.webkitRelativePath) {
          const folderName = firstFile.webkitRelativePath.split("/")[0];
          setSkillName(folderName);
        }
      }
    } else {
      if (e.target.files && e.target.files[0]) {
        setSelectedFile(e.target.files[0]);
      }
    }
  };

  const handleSubmit = async () => {
    if (uploadMode === "folder") {
      if (!selectedFiles || !skillName.trim()) return;

      const formData = new FormData();
      Array.from(selectedFiles).forEach((file) => {
        // Use webkitRelativePath as filename to preserve folder structure
        const relativePath = (file as any).webkitRelativePath || file.name;
        // Append file with custom filename using Blob
        formData.append("files", file, relativePath);
      });
      formData.append("skill_name", skillName.trim());

      try {
        // Use full backend URL instead of relative path
        const API_BASE = "/api";
        const response = await fetch(`${API_BASE}/skills/import`, {
          method: "POST",
          body: formData,
        });

        if (!response.ok) {
          const error = await response.json();
          throw new Error(error.detail || "导入失败");
        }

        const result = await response.json();
        // Trigger parent refresh
        window.location.reload();
      } catch (err) {
        alert(err instanceof Error ? err.message : "导入失败");
      }
    } else {
      if (!selectedFile) return;
      await onImport(selectedFile);
    }
  };

  const getAcceptAttr = () => {
    if (uploadMode === "zip") return ".zip";
    if (uploadMode === "skill") return ".skill";
    return "*";
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/20 backdrop-blur-sm" onClick={onClose} />
      <div className="relative bg-white/95 backdrop-blur-xl rounded-2xl shadow-2xl border border-black/[0.06] w-[520px] p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-blue-500/10 flex items-center justify-center">
              <Upload className="w-4 h-4 text-blue-600" />
            </div>
            <h2 className="text-[15px] font-semibold text-gray-700">导入 Skill</h2>
          </div>
          <button
            onClick={onClose}
            className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-gray-100 transition-colors"
          >
            <X className="w-4 h-4 text-gray-400" />
          </button>
        </div>

        {/* Upload mode selector */}
        <div className="flex gap-2 mb-4">
          <button
            onClick={() => {
              setUploadMode("zip");
              setSelectedFile(null);
              setSelectedFiles(null);
            }}
            className={`flex-1 px-3 py-2 text-[12px] font-medium rounded-lg transition-colors ${
              uploadMode === "zip"
                ? "bg-blue-500 text-white"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            ZIP 压缩包
          </button>
          <button
            onClick={() => {
              setUploadMode("skill");
              setSelectedFile(null);
              setSelectedFiles(null);
            }}
            className={`flex-1 px-3 py-2 text-[12px] font-medium rounded-lg transition-colors ${
              uploadMode === "skill"
                ? "bg-blue-500 text-white"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            .skill 文件
          </button>
          <button
            onClick={() => {
              setUploadMode("folder");
              setSelectedFile(null);
              setSelectedFiles(null);
            }}
            className={`flex-1 px-3 py-2 text-[12px] font-medium rounded-lg transition-colors ${
              uploadMode === "folder"
                ? "bg-blue-500 text-white"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            文件夹
          </button>
        </div>

        {/* Skill name input for folder mode */}
        {uploadMode === "folder" && (
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-gray-500 mb-1.5">
              Skill 名称 <span className="text-gray-400">(自动识别，可修改)</span>
            </label>
            <input
              type="text"
              value={skillName}
              onChange={(e) => setSkillName(e.target.value)}
              className="w-full px-3 py-2 text-[12px] rounded-lg bg-white border border-gray-200 outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-400/20"
              placeholder="选择文件夹后自动识别"
            />
          </div>
        )}

        <div
          onDragEnter={handleDrag}
          onDragLeave={handleDrag}
          onDragOver={handleDrag}
          onDrop={handleDrop}
          className={`border-2 border-dashed rounded-xl p-8 text-center transition-all ${
            dragActive
              ? "border-blue-400 bg-blue-50/50"
              : "border-gray-200 hover:border-blue-300 hover:bg-blue-50/30"
          }`}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept={getAcceptAttr()}
            onChange={handleFileChange}
            multiple={uploadMode === "folder"}
            {...(uploadMode === "folder" ? { webkitdirectory: "" } : {})}
            className="hidden"
          />

          {uploadMode === "folder" && selectedFiles ? (
            <div className="flex flex-col items-center gap-3">
              <div className="w-12 h-12 rounded-lg bg-blue-500/10 flex items-center justify-center">
                <FolderOpen className="w-6 h-6 text-blue-600" />
              </div>
              <div>
                <div className="text-[13px] font-medium text-gray-700">
                  {selectedFiles.length} 个文件
                </div>
                <div className="text-[11px] text-gray-400 mt-0.5">
                  {(Array.from(selectedFiles).reduce((sum, f) => sum + f.size, 0) / 1024 / 1024).toFixed(2)} MB
                </div>
              </div>
              <button
                onClick={() => setSelectedFiles(null)}
                className="text-[12px] text-gray-500 hover:text-gray-700 underline"
              >
                重新选择
              </button>
            </div>
          ) : selectedFile ? (
            <div className="flex flex-col items-center gap-3">
              <div className="w-12 h-12 rounded-lg bg-blue-500/10 flex items-center justify-center">
                <FileText className="w-6 h-6 text-blue-600" />
              </div>
              <div>
                <div className="text-[13px] font-medium text-gray-700">{selectedFile.name}</div>
                <div className="text-[11px] text-gray-400 mt-0.5">
                  {(selectedFile.size / 1024 / 1024).toFixed(2)} MB
                </div>
              </div>
              <button
                onClick={() => setSelectedFile(null)}
                className="text-[12px] text-gray-500 hover:text-gray-700 underline"
              >
                重新选择
              </button>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-3">
              <div className="w-12 h-12 rounded-lg bg-gray-100 flex items-center justify-center">
                {uploadMode === "folder" ? (
                  <FolderOpen className="w-6 h-6 text-gray-400" />
                ) : (
                  <Upload className="w-6 h-6 text-gray-400" />
                )}
              </div>
              <div>
                <div className="text-[13px] font-medium text-gray-700 mb-1">
                  {uploadMode === "zip" && "拖拽 ZIP 文件到此处"}
                  {uploadMode === "skill" && "拖拽 .skill 文件到此处"}
                  {uploadMode === "folder" && "拖拽文件夹到此处"}
                </div>
                <div className="text-[11px] text-gray-400">
                  或{" "}
                  <button
                    onClick={() => fileInputRef.current?.click()}
                    className="text-blue-600 hover:text-blue-700 underline"
                  >
                    点击选择{uploadMode === "folder" ? "文件夹" : "文件"}
                  </button>
                </div>
              </div>
              <div className="text-[10px] text-gray-400 mt-2">
                {uploadMode === "zip" && "仅支持 .zip 格式，最大 50MB"}
                {uploadMode === "skill" && "仅支持 .skill 格式，最大 50MB"}
                {uploadMode === "folder" && "选择包含 SKILL.md 的文件夹"}
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 mt-6">
          <button
            onClick={onClose}
            disabled={importing}
            className="px-4 py-2 text-[12px] font-medium text-gray-600 hover:bg-gray-100 rounded-lg transition-colors disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={
              (uploadMode === "folder" ? !selectedFiles || !skillName.trim() : !selectedFile) ||
              importing
            }
            className="px-4 py-2 text-[12px] font-medium bg-blue-500 text-white rounded-lg hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {importing ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                导入中...
              </>
            ) : (
              <>
                <Upload className="w-3.5 h-3.5" />
                导入
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

// ── Rename Skill Modal ──────────────────────────────────
function RenameSkillModal({
  currentName,
  onClose,
  onRename,
}: {
  currentName: string;
  onClose: () => void;
  onRename: (oldName: string, newName: string) => Promise<void>;
}) {
  const [newName, setNewName] = useState(currentName);
  const [renaming, setRenaming] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const handleSubmit = async () => {
    if (!newName.trim() || newName === currentName) return;
    setRenaming(true);
    try {
      await onRename(currentName, newName.trim());
    } finally {
      setRenaming(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/20 backdrop-blur-sm" onClick={onClose} />
      <div className="relative bg-white/95 backdrop-blur-xl rounded-2xl shadow-2xl border border-black/[0.06] w-[400px] p-5 animate-fade-in-scale">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-purple-500/10 flex items-center justify-center">
              <FileText className="w-4 h-4 text-purple-600" />
            </div>
            <h3 className="text-[14px] font-semibold text-gray-800">重命名 Skill</h3>
          </div>
          <button
            onClick={onClose}
            className="w-7 h-7 flex items-center justify-center rounded-lg hover:bg-black/[0.06] transition-colors"
          >
            <X className="w-4 h-4 text-gray-400" />
          </button>
        </div>

        <div>
          <label className="block text-[11px] font-medium text-gray-500 mb-1.5">
            新名称
          </label>
          <input
            ref={inputRef}
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
            className="w-full px-3 py-2 text-[12px] rounded-lg bg-white border border-gray-200 outline-none focus:border-purple-400 focus:ring-1 focus:ring-purple-400/20"
            placeholder="skill-name"
          />
        </div>

        <div className="flex justify-end gap-2 mt-5">
          <button
            onClick={onClose}
            disabled={renaming}
            className="px-4 py-2 text-[12px] font-medium text-gray-500 hover:bg-black/[0.04] rounded-lg transition-colors disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={renaming || !newName.trim() || newName === currentName}
            className="flex items-center gap-1.5 px-4 py-2 text-[12px] font-medium text-white bg-purple-500 hover:bg-purple-600 rounded-lg transition-colors disabled:opacity-50 shadow-sm shadow-purple-500/15"
          >
            {renaming ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <FileText className="w-3.5 h-3.5" />
            )}
            重命名
          </button>
        </div>
      </div>
    </div>
  );
}
