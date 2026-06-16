"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import dynamic from "next/dynamic";
import "@/lib/monaco-config";
import Navbar from "@/components/layout/Navbar";
import {
  GitCompareArrows,
  Loader2,
  Plus,
  ChevronDown,
  Clock,
  FileText,
  CheckCircle2,
  XCircle,
  Tag,
  ArrowRightLeft,
  X,
} from "lucide-react";
import { listSkills, type SkillInfo } from "@/lib/skillsApi";
import {
  createVersion,
  listVersions,
  diffVersions,
  type VersionInfo,
  type DiffResult,
} from "@/lib/versionApi";
import { getEvalResult, type FiveDimEvalResult } from "@/lib/evalApi";
import { EvalCompareSummary } from "@/components/eval/EvalCompareSummary";

// Dynamic import for Monaco DiffEditor (no SSR)
const DiffEditor = dynamic(
  () => import("@monaco-editor/react").then((mod) => mod.DiffEditor),
  {
    ssr: false,
    loading: () => (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="w-5 h-5 animate-spin text-gray-300" />
      </div>
    ),
  }
);

// ── Main Page ────────────────────────────────────────────
export default function SkillsComparePage() {
  // Skills & versions state
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);
  const [versions, setVersions] = useState<VersionInfo[]>([]);
  const [loadingSkills, setLoadingSkills] = useState(true);
  const [loadingVersions, setLoadingVersions] = useState(false);

  // Diff state
  const [versionA, setVersionA] = useState<string>("");
  const [versionB, setVersionB] = useState<string>("current");
  const [diffResult, setDiffResult] = useState<DiffResult | null>(null);
  const [loadingDiff, setLoadingDiff] = useState(false);

  // Eval results for compare summary
  const [evalResultA, setEvalResultA] = useState<FiveDimEvalResult | null>(null);
  const [evalResultB, setEvalResultB] = useState<FiveDimEvalResult | null>(null);

  // Create version
  const [showCreateModal, setShowCreateModal] = useState(false);

  // Toast
  const [toast, setToast] = useState<{
    type: "success" | "error";
    message: string;
  } | null>(null);

  const showToast = useCallback(
    (type: "success" | "error", message: string) => {
      setToast({ type, message });
      setTimeout(() => setToast(null), 3000);
    },
    []
  );

  // ── Load skills ─────────────────────────────────────
  useEffect(() => {
    (async () => {
      try {
        const data = await listSkills();
        setSkills(data);
      } catch {
        showToast("error", "加载 Skills 列表失败");
      } finally {
        setLoadingSkills(false);
      }
    })();
  }, [showToast]);

  // ── Load versions when skill changes ────────────────
  const loadVersions = useCallback(
    async (skillName: string) => {
      setLoadingVersions(true);
      setVersions([]);
      setDiffResult(null);
      setVersionA("");
      setVersionB("current");
      try {
        const data = await listVersions(skillName);
        setVersions(data);
        if (data.length > 0) {
          setVersionA(data[0].label);
        }
      } catch {
        showToast("error", "加载版本列表失败");
      } finally {
        setLoadingVersions(false);
      }
    },
    [showToast]
  );

  const handleSelectSkill = useCallback(
    (name: string) => {
      setSelectedSkill(name);
      loadVersions(name);
    },
    [loadVersions]
  );

  // ── Run diff ────────────────────────────────────────
  const runDiff = useCallback(async () => {
    if (!selectedSkill || !versionA || !versionB) return;
    setLoadingDiff(true);
    try {
      const result = await diffVersions(selectedSkill, versionA, versionB);
      setDiffResult(result);
    } catch (err) {
      showToast(
        "error",
        err instanceof Error ? err.message : "对比失败"
      );
    } finally {
      setLoadingDiff(false);
    }
  }, [selectedSkill, versionA, versionB, showToast]);

  // Auto-run diff when both versions are selected
  useEffect(() => {
    if (selectedSkill && versionA && versionB) {
      runDiff();
    }
  }, [selectedSkill, versionA, versionB, runDiff]);

  // Load eval results when skill + versions change (with stale-response guard)
  const evalLoadSeqRef = useRef(0);
  useEffect(() => {
    if (!selectedSkill || !versionA || !versionB) return;
    const seq = ++evalLoadSeqRef.current;
    getEvalResult(selectedSkill, versionA === "current" ? undefined : versionA)
      .then((r) => { if (evalLoadSeqRef.current === seq) setEvalResultA(r); })
      .catch(() => { if (evalLoadSeqRef.current === seq) setEvalResultA(null); });
    getEvalResult(selectedSkill, versionB === "current" ? undefined : versionB)
      .then((r) => { if (evalLoadSeqRef.current === seq) setEvalResultB(r); })
      .catch(() => { if (evalLoadSeqRef.current === seq) setEvalResultB(null); });
  }, [selectedSkill, versionA, versionB]);

  // ── Create version handler ──────────────────────────
  const handleCreateVersion = useCallback(
    async (label: string) => {
      if (!selectedSkill) return;
      try {
        await createVersion(selectedSkill, label);
        showToast("success", `版本 "${label}" 创建成功`);
        setShowCreateModal(false);
        await loadVersions(selectedSkill);
      } catch (err) {
        showToast(
          "error",
          err instanceof Error ? err.message : "创建版本失败"
        );
      }
    },
    [selectedSkill, showToast, loadVersions]
  );

  // ── Diff statistics ─────────────────────────────────
  const diffStats = computeDiffStats(diffResult);

  // Version options for selectors
  const versionOptions = [
    ...versions.map((v) => v.label),
    "current",
  ];

  return (
    <div className="h-screen flex flex-col app-bg">
      <Navbar />
      <div className="flex-1 flex overflow-hidden">
        {/* ── Left Panel: Skill Select + Version Timeline ─── */}
        <div className="w-[220px] glass-panel border-r border-black/[0.06] shrink-0 flex flex-col">
          {/* Skill Selector */}
          <div className="p-3 border-b border-black/[0.06]">
            <div className="flex items-center gap-2 mb-2.5">
              <GitCompareArrows className="w-4 h-4 text-violet-500" />
              <span className="text-[13px] font-semibold text-gray-700">
                版本对比
              </span>
            </div>
            <div className="relative">
              <select
                value={selectedSkill ?? ""}
                onChange={(e) => {
                  if (e.target.value) handleSelectSkill(e.target.value);
                }}
                className="form-select text-[12px] pr-8"
              >
                <option value="" disabled>
                  选择 Skill...
                </option>
                {skills.map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Version Timeline */}
          <div className="flex-1 overflow-y-auto p-2 space-y-1">
            {loadingSkills || loadingVersions ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="w-5 h-5 animate-spin text-gray-300" />
              </div>
            ) : !selectedSkill ? (
              <div className="flex flex-col items-center justify-center py-12 text-gray-400">
                <GitCompareArrows className="w-8 h-8 mb-2 opacity-40" />
                <span className="text-[11px] text-center leading-relaxed">
                  请先选择一个 Skill
                </span>
              </div>
            ) : versions.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-gray-400">
                <Tag className="w-8 h-8 mb-2 opacity-40" />
                <span className="text-[11px] text-center leading-relaxed">
                  暂无版本快照
                </span>
                <span className="text-[10px] text-gray-400 mt-1">
                  点击下方按钮创建
                </span>
              </div>
            ) : (
              versions.map((ver, idx) => (
                <div
                  key={ver.label}
                  className={`flex items-start gap-2.5 px-2.5 py-2 rounded-lg transition-all ${
                    versionA === ver.label
                      ? "bg-violet-500/10 border border-violet-400/20"
                      : "hover:bg-white/50 border border-transparent"
                  }`}
                >
                  {/* Timeline dot */}
                  <div className="flex flex-col items-center mt-1">
                    <div
                      className={`w-2.5 h-2.5 rounded-full ${
                        versionA === ver.label
                          ? "bg-violet-500"
                          : "bg-gray-300"
                      }`}
                    />
                    {idx < versions.length - 1 && (
                      <div className="w-px h-6 bg-gray-200 mt-1" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <button
                      onClick={() => setVersionA(ver.label)}
                      className="text-[12px] font-medium text-gray-700 truncate block w-full text-left hover:text-violet-600 transition-colors"
                    >
                      {ver.label}
                    </button>
                    <div className="flex items-center gap-2 mt-0.5 text-[10px] text-gray-400">
                      <span className="flex items-center gap-0.5">
                        <Clock className="w-2.5 h-2.5" />
                        {formatTime(ver.created_at)}
                      </span>
                      <span className="flex items-center gap-0.5">
                        <FileText className="w-2.5 h-2.5" />
                        {ver.file_count} 文件
                      </span>
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>

          {/* Create Version Button */}
          {selectedSkill && (
            <div className="p-3 border-t border-black/[0.06]">
              <button
                onClick={() => setShowCreateModal(true)}
                className="w-full flex items-center justify-center gap-1.5 px-3 py-2 text-[12px] font-medium text-white bg-violet-500 hover:bg-violet-600 rounded-lg transition-colors shadow-sm shadow-violet-500/15"
              >
                <Plus className="w-3.5 h-3.5" />
                创建版本快照
              </button>
            </div>
          )}
        </div>

        {/* ── Center: Diff Editor ──────────────────────────── */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Toolbar */}
          <div className="h-10 flex items-center justify-between px-3 border-b border-black/[0.06] bg-white/40 shrink-0">
            <div className="flex items-center gap-2">
              {/* Version A selector */}
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] font-medium text-gray-400 uppercase">
                  A
                </span>
                <select
                  value={versionA}
                  onChange={(e) => setVersionA(e.target.value)}
                  disabled={!selectedSkill || versions.length === 0}
                  className="px-2 py-1 text-[11px] rounded-md border border-black/[0.06] bg-white/60 outline-none focus:border-violet-400 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <option value="" disabled>
                    选择版本
                  </option>
                  {versionOptions.map((v) => (
                    <option key={`a-${v}`} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </div>

              <ArrowRightLeft className="w-3.5 h-3.5 text-gray-400" />

              {/* Version B selector */}
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] font-medium text-gray-400 uppercase">
                  B
                </span>
                <select
                  value={versionB}
                  onChange={(e) => setVersionB(e.target.value)}
                  disabled={!selectedSkill || versions.length === 0}
                  className="px-2 py-1 text-[11px] rounded-md border border-black/[0.06] bg-white/60 outline-none focus:border-violet-400 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <option value="" disabled>
                    选择版本
                  </option>
                  {versionOptions.map((v) => (
                    <option key={`b-${v}`} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Diff stats */}
            <div className="flex items-center gap-3">
              {loadingDiff && (
                <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-400" />
              )}
              {diffResult && !loadingDiff && (
                <div className="flex items-center gap-2 text-[11px] font-mono">
                  <span className="text-emerald-600">
                    +{diffStats.added}
                  </span>
                  <span className="text-red-500">
                    -{diffStats.removed}
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* Diff Editor Area */}
          <div className="flex-1 min-w-0">
            {!selectedSkill ? (
              <EmptyState />
            ) : !diffResult && !loadingDiff ? (
              <div className="flex-1 flex items-center justify-center h-full">
                <div className="text-center">
                  <ArrowRightLeft className="w-10 h-10 text-gray-300 mx-auto mb-3" />
                  <p className="text-[13px] text-gray-400">
                    选择两个版本开始对比
                  </p>
                </div>
              </div>
            ) : loadingDiff ? (
              <div className="flex-1 flex items-center justify-center h-full">
                <Loader2 className="w-6 h-6 animate-spin text-violet-400" />
              </div>
            ) : diffResult ? (
              <DiffEditor
                height="100%"
                language="markdown"
                theme="vs"
                original={diffResult.content_a}
                modified={diffResult.content_b}
                options={{
                  fontSize: 13,
                  fontFamily:
                    "'SF Mono', 'JetBrains Mono', 'Fira Code', Consolas, monospace",
                  lineHeight: 22,
                  minimap: { enabled: false },
                  wordWrap: "on",
                  padding: { top: 12, bottom: 12 },
                  scrollBeyondLastLine: false,
                  renderSideBySide: true,
                  readOnly: true,
                  originalEditable: false,
                  overviewRulerBorder: false,
                  glyphMargin: false,
                  contextmenu: false,
                }}
              />
            ) : null}
          </div>

          {/* Eval compare summary (below diff editor) */}
          {selectedSkill && versionA && versionB && (evalResultA || evalResultB) && (
            <div className="px-4 py-3 border-t border-black/[0.06]">
              <EvalCompareSummary
                resultA={evalResultA}
                resultB={evalResultB}
                versionA={versionA}
                versionB={versionB}
                onViewFull={() => {
                  window.location.href = `/skills/review?skill=${encodeURIComponent(selectedSkill)}&vA=${encodeURIComponent(versionA)}&vB=${encodeURIComponent(versionB)}`;
                }}
              />
            </div>
          )}
        </div>
      </div>

      {/* ── Create Version Modal ─────────────────────────── */}
      {showCreateModal && (
        <CreateVersionModal
          onClose={() => setShowCreateModal(false)}
          onCreate={handleCreateVersion}
        />
      )}

      {/* ── Toast ────────────────────────────────────────── */}
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

// ── Empty State ──────────────────────────────────────────
function EmptyState() {
  return (
    <div className="flex-1 flex items-center justify-center h-full">
      <div className="text-center">
        <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-violet-400 to-violet-500 flex items-center justify-center mx-auto mb-4 shadow-lg shadow-violet-400/15">
          <GitCompareArrows className="w-8 h-8 text-white" />
        </div>
        <h2 className="text-[15px] font-semibold text-gray-700 mb-1">
          版本对比
        </h2>
        <p className="text-[12px] text-gray-400 max-w-[260px] mx-auto leading-relaxed">
          从左侧选择一个 Skill，创建版本快照后即可对比不同版本的差异
        </p>
      </div>
    </div>
  );
}

// ── Create Version Modal ─────────────────────────────────
function CreateVersionModal({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (label: string) => Promise<void>;
}) {
  const [label, setLabel] = useState("");
  const [creating, setCreating] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleSubmit = async () => {
    if (!label.trim()) return;
    setCreating(true);
    try {
      await onCreate(label.trim());
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
      <div className="relative bg-white/95 backdrop-blur-xl rounded-2xl shadow-2xl border border-black/[0.06] w-[380px] p-5 animate-fade-in-scale">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-violet-500/10 flex items-center justify-center">
              <Tag className="w-4 h-4 text-violet-600" />
            </div>
            <h3 className="text-[14px] font-semibold text-gray-800">
              创建版本快照
            </h3>
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
            版本标签
          </label>
          <input
            ref={inputRef}
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
            className="form-input"
            placeholder="v1.0"
          />
          <p className="text-[10px] text-gray-400 mt-1.5">
            仅允许字母、数字、点和连字符
          </p>
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
            disabled={creating || !label.trim()}
            className="flex items-center gap-1.5 px-4 py-2 text-[12px] font-medium text-white bg-violet-500 hover:bg-violet-600 rounded-lg transition-colors disabled:opacity-50 shadow-sm shadow-violet-500/15"
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

// ── Helpers ──────────────────────────────────────────────

function formatTime(isoStr: string): string {
  try {
    const d = new Date(isoStr);
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const hour = String(d.getHours()).padStart(2, "0");
    const min = String(d.getMinutes()).padStart(2, "0");
    return `${month}-${day} ${hour}:${min}`;
  } catch {
    return isoStr;
  }
}

function computeDiffStats(result: DiffResult | null): {
  added: number;
  removed: number;
} {
  if (!result) return { added: 0, removed: 0 };

  const linesA = result.content_a.split("\n");
  const linesB = result.content_b.split("\n");

  const setA = new Set(linesA);
  const setB = new Set(linesB);

  let added = 0;
  let removed = 0;

  for (const line of linesB) {
    if (!setA.has(line)) added++;
  }
  for (const line of linesA) {
    if (!setB.has(line)) removed++;
  }

  return { added, removed };
}
