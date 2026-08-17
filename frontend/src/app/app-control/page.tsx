"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertCircle,
  ArrowRight,
  BarChart3,
  BookOpen,
  Bot,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Loader2,
  RefreshCw,
  Terminal,
} from "lucide-react";
import type {
  OnboardingProfileId,
  OnboardingState,
  ProfileDependency,
  ProfileInspection,
} from "@/types/electron";

type BackendStatus = { status: string; error: string | null; url: string };
type InfraStatus = {
  docker: boolean;
  postgres: string;
  milvus: string;
  status: string;
  error: string | null;
};

const PROFILE_OPTIONS: Array<{
  id: OnboardingProfileId;
  title: string;
  eyebrow: string;
  description: string;
  features: string[];
  icon: typeof Bot;
  recommended?: boolean;
}> = [
  {
    id: "harness",
    title: "Harness 模式",
    eyebrow: "轻量起步",
    description: "专注 Agent、工具、上下文、任务与本地文件，不安装知识库和问数依赖。",
    features: ["Agent 对话与工具", "Goal / Todo / 验收", "本地文件与终端"],
    icon: Bot,
    recommended: true,
  },
  {
    id: "knowledge",
    title: "知识库模式",
    eyebrow: "完整知识 Pipeline",
    description: "覆盖知识接入、解析、切分、索引、检索与引用，并提供持续更新的 LLM Wiki。",
    features: ["完整 Pipeline", "LLM Wiki", "溯源引用"],
    icon: BookOpen,
  },
  {
    id: "full",
    title: "知识库 + 问数",
    eyebrow: "完整工作台",
    description: "在完整知识 Pipeline 与 LLM Wiki 之上，增加文件、数据库和分析问数能力。",
    features: ["完整 Pipeline", "LLM Wiki", "智能问数"],
    icon: BarChart3,
  },
];

const GROUP_LABELS: Record<ProfileDependency["group"], string> = {
  core: "必需运行环境",
  configuration: "进入后配置",
  optional: "可选增强",
  knowledge: "知识库依赖",
  analytics: "问数依赖",
};

const STATUS_META: Record<string, { label: string; className: string; icon: typeof CheckCircle2 }> = {
  available: { label: "可用", className: "text-emerald-700 bg-emerald-50 border-emerald-200", icon: CheckCircle2 },
  planned: { label: "将自动创建", className: "text-blue-700 bg-blue-50 border-blue-200", icon: CircleDashed },
  needs_action: { label: "待准备", className: "text-amber-700 bg-amber-50 border-amber-200", icon: AlertCircle },
  not_configured: { label: "稍后配置", className: "text-slate-600 bg-slate-50 border-slate-200", icon: CircleDashed },
  optional_unavailable: { label: "未启用（可选）", className: "text-slate-600 bg-slate-50 border-slate-200", icon: CircleDashed },
};

function webPreviewInspection(profile: OnboardingProfileId): ProfileInspection {
  const knowledge = profile === "knowledge" || profile === "full";
  const analytics = profile === "full";
  const dependencies: ProfileDependency[] = [
    { id: "runtime.cli", label: "PuddingClaw 客户端组件", group: "core", required: true, status: "available", detail: "桌面客户端运行环境已就绪", remediation: [], source: "cli" },
    { id: "runtime.node", label: "Node.js 20+ Runtime", group: "core", required: true, status: "available", detail: "示例：客户端内置 Node.js Runtime", remediation: [], source: "cli" },
    { id: "runtime.python", label: "Python 3.11 / 3.12", group: "core", required: true, status: "available", detail: "示例：Python 3.12 · 客户端受管理环境", remediation: [], source: "cli" },
    { id: "runtime.uv", label: "uv 依赖管理器", group: "core", required: true, status: "available", detail: "示例：uv 已就绪", remediation: [], source: "cli" },
    { id: "catalog.sqlite", label: "SQLite Core Catalog", group: "core", required: true, status: "available", detail: "默认本地数据库；不要求 PostgreSQL 或 Docker", remediation: [], source: "cli" },
    { id: "provider.agent", label: "Agent 模型 Provider", group: "configuration", required: false, status: "not_configured", detail: "进入设置后绑定模型与凭据", remediation: ["在模型服务设置中完成绑定"], source: "cli" },
    { id: "runtime.docker", label: "Docker 沙箱", group: "optional", required: false, status: "optional_unavailable", detail: "可选；不可用时回退到内核沙箱", remediation: ["需要容器隔离时启动 Docker Desktop"], source: "cli" },
  ];
  if (knowledge) {
    dependencies.push(
      { id: "provider.multimodal", label: "Embedding / 多模态模型", group: "knowledge", required: false, status: "not_configured", detail: "启用图文向量检索时配置；精确检索不依赖", remediation: ["需要语义检索时再绑定"], source: "cli" },
      { id: "knowledge.milvus", label: "Milvus 向量库", group: "knowledge", required: false, status: "optional_unavailable", detail: "可选；不可用时仍可使用文件与精确检索", remediation: ["如需向量检索，可稍后在知识库设置中启用"], source: "cli" },
      { id: "knowledge.mineru", label: "MinerU 富文档解析", group: "knowledge", required: false, status: "optional_unavailable", detail: "可选；只影响 PDF/Office 高质量解析", remediation: ["需要时启动 MinerU"], source: "cli" },
    );
  }
  if (analytics) {
    dependencies.push(
      { id: "analytics.datasource", label: "问数数据源", group: "analytics", required: false, status: "not_configured", detail: "支持文件数据；数据库连接可在进入应用后添加", remediation: ["导入文件或配置只读数据库连接"], source: "cli" },
      { id: "analytics.postgres_driver", label: "PostgreSQL 数据源驱动", group: "analytics", required: false, status: "not_configured", detail: "只有连接 PostgreSQL 业务数据源时需要", remediation: ["连接 PostgreSQL 数据源时按需准备"], source: "cli" },
    );
  }
  return {
    schema_version: 1,
    status: "ready",
    profile,
    label: PROFILE_OPTIONS.find((item) => item.id === profile)?.title || profile,
    initialized: false,
    current_profile: null,
    extensions: { knowledge, analytics, headless_worker: true },
    dependency_profile: profile,
    dependencies,
    blocking: [],
    actions: { can_apply: false, can_prepare: false },
  };
}

function DependencyRow({ dependency }: { dependency: ProfileDependency }) {
  const meta = STATUS_META[dependency.status] || STATUS_META.not_configured;
  const Icon = meta.icon;
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <p className="text-sm font-medium text-slate-900">{dependency.label}</p>
            {dependency.required && (
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">必需</span>
            )}
          </div>
          <p className="mt-1 break-all text-xs leading-5 text-slate-500">{dependency.detail}</p>
          {dependency.remediation.length > 0 && dependency.status !== "available" && (
            <p className="mt-1.5 text-xs leading-5 text-amber-700">下一步：{dependency.remediation.join("；")}</p>
          )}
        </div>
        <span className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-1 text-[11px] font-medium ${meta.className}`}>
          <Icon className="h-3 w-3" />
          {meta.label}
        </span>
      </div>
    </div>
  );
}

function ProfileSetup({
  selected,
  inspection,
  loading,
  applying,
  error,
  onSelect,
  onRefresh,
  onApply,
  onCancel,
  preview = false,
}: {
  selected: OnboardingProfileId;
  inspection: ProfileInspection | null;
  loading: boolean;
  applying: boolean;
  error: string | null;
  onSelect: (profile: OnboardingProfileId) => void;
  onRefresh: () => void;
  onApply: () => void;
  onCancel?: () => void;
  preview?: boolean;
}) {
  const [dependenciesExpanded, setDependenciesExpanded] = useState(false);
  const [activeGroup, setActiveGroup] = useState<ProfileDependency["group"]>("core");
  const groups = useMemo(() => {
    const result = new Map<ProfileDependency["group"], ProfileDependency[]>();
    for (const dependency of inspection?.dependencies || []) {
      const current = result.get(dependency.group) || [];
      current.push(dependency);
      result.set(dependency.group, current);
    }
    return result;
  }, [inspection]);
  const groupEntries = useMemo(() => Array.from(groups.entries()), [groups]);
  const dependencySummary = useMemo(() => {
    const summary = { ready: 0, prepare: 0, later: 0 };
    for (const dependency of inspection?.dependencies || []) {
      if (dependency.status === "available") summary.ready += 1;
      else if (dependency.status === "planned" || dependency.status === "needs_action") summary.prepare += 1;
      else summary.later += 1;
    }
    return summary;
  }, [inspection]);

  useEffect(() => {
    setDependenciesExpanded(false);
  }, [selected]);

  useEffect(() => {
    if (groupEntries.length > 0 && !groups.has(activeGroup)) {
      setActiveGroup(groupEntries[0][0]);
    }
  }, [activeGroup, groupEntries, groups]);

  const activeDependencies = groups.get(activeGroup) || groupEntries[0]?.[1] || [];
  const activeGroupLabel = groups.has(activeGroup)
    ? GROUP_LABELS[activeGroup]
    : groupEntries[0]
      ? GROUP_LABELS[groupEntries[0][0]]
      : "依赖详情";
  const selectedOption = PROFILE_OPTIONS.find((option) => option.id === selected) || PROFILE_OPTIONS[0];

  return (
    <div className="grid h-full min-h-0 grid-rows-[minmax(0,1fr)_auto] overflow-hidden bg-[#f6f7fb]">
      <div className="min-h-0 overflow-y-auto px-5 py-6 sm:px-8">
        <div className="mx-auto max-w-7xl">
          <header>
            <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-[#002fa7]/15 bg-[#002fa7]/5 px-3 py-1 text-xs font-medium text-[#002fa7]">
              <Terminal className="h-3.5 w-3.5" />
              {preview ? "Web 只读预览" : "首次启动 · 环境检测"}
            </div>
            <h1 className="text-3xl font-semibold tracking-tight text-slate-950">选择你的 PuddingClaw 工作模式</h1>
            <p className="mt-2 max-w-4xl text-sm leading-6 text-slate-600">
              先选择要使用的产品能力。客户端会检查对应环境；详细依赖按需展开，后续仍可修改模式。
            </p>
          </header>

          <section className="mt-5 grid gap-3 lg:grid-cols-3" aria-label="工作模式">
            {PROFILE_OPTIONS.map((option) => {
              const Icon = option.icon;
              const active = selected === option.id;
              return (
                <button
                  key={option.id}
                  type="button"
                  aria-pressed={active}
                  onClick={() => onSelect(option.id)}
                  className={`flex min-h-[220px] w-full flex-col rounded-2xl border p-5 text-left transition-all ${
                    active
                      ? "border-[#002fa7] bg-white shadow-[0_12px_35px_rgba(0,47,167,0.10)] ring-1 ring-[#002fa7]/10"
                      : "border-slate-200 bg-white/80 hover:-translate-y-0.5 hover:border-slate-300 hover:bg-white hover:shadow-sm"
                  }`}
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl ${active ? "bg-[#002fa7] text-white" : "bg-slate-100 text-slate-600"}`}>
                      <Icon className="h-5 w-5" />
                    </div>
                    <div className={`mt-1 h-5 w-5 rounded-full border-2 ${active ? "border-[#002fa7] bg-[#002fa7] shadow-[inset_0_0_0_4px_white]" : "border-slate-300"}`} />
                  </div>
                  <div className="mt-4 flex flex-wrap items-center gap-2">
                    <p className="text-xs font-medium tracking-[0.08em] text-slate-400">{option.eyebrow}</p>
                    {option.recommended && (
                      <span className="rounded-full bg-[#002fa7]/10 px-2 py-0.5 text-[10px] font-medium text-[#002fa7]">推荐首次体验</span>
                    )}
                  </div>
                  <h2 className="mt-1.5 text-lg font-semibold text-slate-950">{option.title}</h2>
                  <p className="mt-1.5 text-sm leading-6 text-slate-600">{option.description}</p>
                  <div className="mt-auto flex flex-wrap gap-2 pt-4">
                    {option.features.map((feature) => (
                      <span key={feature} className="rounded-lg bg-slate-100 px-2.5 py-1 text-xs text-slate-600">{feature}</span>
                    ))}
                  </div>
                </button>
              );
            })}
          </section>

          <section className="mt-4 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm" aria-label="环境准备">
            <div className="flex min-h-[76px] flex-wrap items-center gap-4 px-4 py-3 sm:px-5">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[#002fa7]/5 text-[#002fa7]">
                {loading ? <Loader2 className="h-5 w-5 animate-spin" /> : <Terminal className="h-5 w-5" />}
              </div>
              <div className="min-w-[220px] flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold text-slate-950">环境准备概览</h3>
                  <span className="text-xs font-medium text-[#002fa7]">{selectedOption.title}</span>
                </div>
                {inspection ? (
                  <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                    <span className="inline-flex items-center gap-1.5 text-emerald-700"><span className="h-1.5 w-1.5 rounded-full bg-current" />{dependencySummary.ready} 项已就绪</span>
                    <span className="inline-flex items-center gap-1.5 text-amber-700"><span className="h-1.5 w-1.5 rounded-full bg-current" />{dependencySummary.prepare} 项将自动准备</span>
                    <span className="inline-flex items-center gap-1.5 text-slate-500"><span className="h-1.5 w-1.5 rounded-full bg-current" />{dependencySummary.later} 项可稍后配置</span>
                  </div>
                ) : (
                  <p className="mt-1.5 text-xs text-slate-500">正在检测所选模式的运行环境…</p>
                )}
              </div>
              <div className="ml-auto flex items-center gap-2">
                <button
                  type="button"
                  onClick={onRefresh}
                  disabled={loading}
                  className="inline-flex h-10 items-center gap-1.5 rounded-xl border border-slate-200 px-3 text-xs font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
                  重测
                </button>
                <button
                  type="button"
                  onClick={() => setDependenciesExpanded((expanded) => !expanded)}
                  aria-expanded={dependenciesExpanded}
                  className="inline-flex h-10 items-center gap-2 rounded-xl border border-slate-200 px-3 text-xs font-medium text-slate-600 hover:bg-slate-50"
                >
                  {dependenciesExpanded ? "收起详情" : "展开依赖详情"}
                  <ChevronDown className={`h-3.5 w-3.5 transition-transform ${dependenciesExpanded ? "rotate-180" : ""}`} />
                </button>
              </div>
            </div>

            {error && (
              <div className="border-t border-red-100 bg-red-50 px-5 py-3 text-xs leading-5 text-red-700">{error}</div>
            )}

            <div className={`grid border-t transition-[grid-template-rows,border-color] duration-200 ${dependenciesExpanded ? "grid-rows-[1fr] border-slate-200" : "grid-rows-[0fr] border-transparent"}`}>
              <div className="min-h-0 overflow-hidden">
                {loading && !inspection ? (
                  <div className="flex h-32 items-center justify-center gap-2 text-sm text-slate-500">
                    <Loader2 className="h-4 w-4 animate-spin" /> 正在检测本机环境…
                  </div>
                ) : groupEntries.length > 0 ? (
                  <div className="grid max-h-72 min-h-0 sm:grid-cols-[11rem_minmax(0,1fr)]">
                    <nav className="flex gap-1 overflow-x-auto border-b border-slate-200 bg-slate-50/80 p-3 sm:block sm:overflow-visible sm:border-b-0 sm:border-r" aria-label="依赖分类">
                      {groupEntries.map(([group, dependencies]) => (
                        <button
                          key={group}
                          type="button"
                          onClick={() => setActiveGroup(group)}
                          className={`flex h-9 shrink-0 items-center justify-between gap-3 rounded-lg px-3 text-left text-xs transition sm:mb-1 sm:w-full ${
                            activeGroup === group
                              ? "bg-[#002fa7]/10 font-medium text-[#002fa7]"
                              : "text-slate-600 hover:bg-slate-100"
                          }`}
                        >
                          <span>{GROUP_LABELS[group]}</span>
                          <span className="rounded-full bg-slate-200/70 px-1.5 py-0.5 text-[10px] text-slate-500">{dependencies.length}</span>
                        </button>
                      ))}
                    </nav>
                    <div className="min-h-0 overflow-y-auto p-4">
                      <div className="mb-3 flex items-center justify-between gap-3">
                        <div>
                          <h4 className="text-sm font-semibold text-slate-900">{activeGroupLabel}</h4>
                          <p className="mt-1 text-xs text-slate-500">
                            {preview ? "当前展示示例结果；桌面客户端会检测真实环境。" : "检测只读取当前环境，不会修改系统配置。"}
                          </p>
                        </div>
                        <span className="shrink-0 text-[11px] text-slate-400">{activeDependencies.length} 项</span>
                      </div>
                      <div className="space-y-2">
                        {activeDependencies.map((dependency) => <DependencyRow key={dependency.id} dependency={dependency} />)}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="flex h-28 items-center justify-center text-sm text-slate-500">暂无依赖检测结果</div>
                )}
              </div>
            </div>
          </section>
        </div>
      </div>

      <footer className="border-t border-slate-200 bg-white/95 shadow-[0_-10px_32px_rgba(15,23,42,0.05)] backdrop-blur">
        <div className="mx-auto flex min-h-[76px] max-w-7xl flex-wrap items-center justify-between gap-4 px-5 py-3 sm:px-8">
          <div className="min-w-0">
            <p className="text-[11px] text-slate-400">当前选择</p>
            <p className="mt-1 text-sm font-semibold text-slate-900">
              {selectedOption.title}
              <span className={`ml-2 font-medium ${inspection?.blocking.length ? "text-amber-700" : "text-emerald-700"}`}>
                {loading ? "· 检测中" : inspection?.blocking.length ? `· ${inspection.blocking.length} 项待准备` : "· 可继续"}
              </span>
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            {onCancel && (
              <button type="button" onClick={onCancel} className="h-11 rounded-xl border border-slate-200 px-4 text-sm font-medium text-slate-600 hover:bg-slate-50">
                返回
              </button>
            )}
            <button
              type="button"
              onClick={() => setDependenciesExpanded((expanded) => !expanded)}
              className="hidden h-11 rounded-xl border border-slate-200 px-4 text-sm font-medium text-slate-600 hover:bg-slate-50 sm:inline-flex sm:items-center"
            >
              {dependenciesExpanded ? "收起环境详情" : "查看环境详情"}
            </button>
            <button
              type="button"
              onClick={onApply}
              disabled={preview || loading || applying || !inspection}
              className="inline-flex h-11 items-center justify-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-medium text-white shadow-[0_8px_18px_rgba(0,47,167,0.16)] transition hover:bg-[#00257f] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {applying ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowRight className="h-4 w-4" />}
              {preview
                ? "Web 预览不执行配置"
                : applying
                  ? "正在准备所选模式…"
                  : "确认选择并继续"}
            </button>
          </div>
        </div>
      </footer>
    </div>
  );
}

export default function AppControlPage() {
  const router = useRouter();
  const [isElectron, setIsElectron] = useState<boolean | null>(null);
  const [isWebPreview, setIsWebPreview] = useState(false);
  const [onboarding, setOnboarding] = useState<OnboardingState | null>(null);
  const [showProfileSetup, setShowProfileSetup] = useState(false);
  const [selectedProfile, setSelectedProfile] = useState<OnboardingProfileId>("harness");
  const [inspection, setInspection] = useState<ProfileInspection | null>(null);
  const [inspectionLoading, setInspectionLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [backendStatus, setBackendStatus] = useState<BackendStatus | null>(null);
  const [infraStatus, setInfraStatus] = useState<InfraStatus | null>(null);
  const [loading, setLoading] = useState({ backend: false, infra: false });
  const inspectionRequest = useRef(0);

  const inspect = async (profile: OnboardingProfileId) => {
    if (!window.electron) return;
    const requestId = inspectionRequest.current + 1;
    inspectionRequest.current = requestId;
    setInspectionLoading(true);
    setProfileError(null);
    try {
      const result = await window.electron.inspectOnboardingProfile(profile);
      if (inspectionRequest.current === requestId) setInspection(result);
    } catch (error) {
      if (inspectionRequest.current === requestId) {
        setInspection(null);
        setProfileError(error instanceof Error ? error.message : "环境检测失败");
      }
    } finally {
      if (inspectionRequest.current === requestId) setInspectionLoading(false);
    }
  };

  useEffect(() => {
    const electron = typeof window !== "undefined" && Boolean(window.electron);
    const webPreview = typeof window !== "undefined"
      && new URLSearchParams(window.location.search).get("preview") === "onboarding";
    setIsElectron(electron);
    setIsWebPreview(webPreview);
    if (!electron) {
      if (webPreview) {
        setOnboarding({ available: false, initialized: false, profile: null, extensions: null, home: "Web 预览" });
        setInspection(webPreviewInspection("harness"));
        setShowProfileSetup(true);
      }
      return;
    }

    void window.electron?.getOnboardingState().then((state) => {
      setOnboarding(state);
      const visibleProfile: OnboardingProfileId = state.profile === "knowledge"
        ? "knowledge"
        : state.profile === "full" || state.profile === "analytics"
          ? "full"
          : "harness";
      setSelectedProfile(visibleProfile);
      setShowProfileSetup(!state.initialized);
      void inspect(visibleProfile);
      if (state.initialized) {
        void window.electron?.getBackendStatus().then((status) => {
          setBackendStatus(status);
          if (status.status !== "running") {
            setLoading((previous) => ({ ...previous, backend: true }));
            void window.electron?.startBackend().finally(() => {
              setLoading((previous) => ({ ...previous, backend: false }));
            });
          }
        });
      }
    });
    void window.electron?.getBackendStatus().then(setBackendStatus);
    void window.electron?.getInfraStatus().then(setInfraStatus);

    const handleBackendStatus = (_event: unknown, status: unknown) => setBackendStatus(status as BackendStatus);
    const handleInfraStatus = (_event: unknown, status: unknown) => setInfraStatus(status as InfraStatus);
    window.electron?.onBackendStatusChange(handleBackendStatus);
    window.electron?.onInfraStatusChange(handleInfraStatus);
    return () => {
      window.electron?.removeAllListeners("backend-status-change");
      window.electron?.removeAllListeners("infra-status-change");
    };
  }, []);

  const selectProfile = (profile: OnboardingProfileId) => {
    setSelectedProfile(profile);
    if (isWebPreview) {
      setInspection(webPreviewInspection(profile));
      return;
    }
    setInspection(null);
    void inspect(profile);
  };

  const applySelectedProfile = async () => {
    if (!window.electron) return;
    setApplying(true);
    setProfileError(null);
    try {
      const result = await window.electron.applyOnboardingProfile(selectedProfile);
      setOnboarding({
        available: true,
        initialized: true,
        profile: result.profile,
        extensions: result.extensions,
        home: onboarding?.home || result.config_path,
      });
      setBackendStatus({ status: result.backend.status, error: result.backend.status === "error" ? result.backend.message : null, url: "http://127.0.0.1:8888" });
      setInspection(result.inspection);
      setShowProfileSetup(false);
    } catch (error) {
      setProfileError(error instanceof Error ? error.message : "应用模式失败");
    } finally {
      setApplying(false);
    }
  };

  const handleStopBackend = async () => {
    if (!window.electron) return;
    setLoading((previous) => ({ ...previous, backend: true }));
    await window.electron.stopBackend();
    setLoading((previous) => ({ ...previous, backend: false }));
  };

  const handleStartInfra = async () => {
    if (!window.electron) return;
    setLoading((previous) => ({ ...previous, infra: true }));
    await window.electron.startInfra();
    setLoading((previous) => ({ ...previous, infra: false }));
  };

  const handleStopInfra = async () => {
    if (!window.electron) return;
    setLoading((previous) => ({ ...previous, infra: true }));
    await window.electron.stopInfra();
    setLoading((previous) => ({ ...previous, infra: false }));
  };

  if (isElectron === null || (isElectron && onboarding === null)) {
    return <div className="flex h-full overflow-y-auto items-center justify-center bg-[#f6f7fb] text-sm text-slate-500"><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在读取首次启动状态…</div>;
  }
  if (!isElectron && !isWebPreview) {
    return (
      <div className="flex h-full overflow-y-auto items-center justify-center bg-gray-50 p-8">
        <div className="w-full max-w-md rounded-xl border border-gray-200 bg-white p-6 text-center shadow-sm">
          <h1 className="mb-2 text-lg font-semibold text-gray-900">请在 Electron 中运行</h1>
          <p className="text-sm text-gray-600">首次模式选择需要在 PuddingClaw 桌面客户端中完成。</p>
        </div>
      </div>
    );
  }
  if (showProfileSetup) {
    return (
      <ProfileSetup
        selected={selectedProfile}
        inspection={inspection}
        loading={inspectionLoading}
        applying={applying}
        error={profileError || onboarding?.error || null}
        onSelect={selectProfile}
        onRefresh={() => void inspect(selectedProfile)}
        onApply={() => void applySelectedProfile()}
        onCancel={onboarding?.initialized ? () => setShowProfileSetup(false) : undefined}
        preview={isWebPreview}
      />
    );
  }

  const currentOption = PROFILE_OPTIONS.find((option) => option.id === selectedProfile) || PROFILE_OPTIONS[0];
  const statusColor: Record<string, string> = {
    running: "text-green-600 bg-green-50 border-green-200",
    stopped: "text-gray-500 bg-gray-50 border-gray-200",
    starting: "text-yellow-600 bg-yellow-50 border-yellow-200",
    error: "text-red-600 bg-red-50 border-red-200",
    partial: "text-yellow-600 bg-yellow-50 border-yellow-200",
    unknown: "text-gray-500 bg-gray-50 border-gray-200",
  };
  const infraServiceStateLabel: Record<string, string> = {
    running: "运行中", stopped: "未运行", error: "异常", not_required: "无需启动",
  };

  return (
    <div className="h-full overflow-y-auto bg-gray-50 p-8">
      <div className="mx-auto max-w-3xl space-y-6">
        <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="text-xs font-medium uppercase tracking-[0.14em] text-[#002fa7]">当前工作模式</p>
              <h1 className="mt-1 text-2xl font-semibold text-gray-900">{currentOption.title}</h1>
              <p className="mt-2 text-sm text-gray-500">{currentOption.description}</p>
            </div>
            <button onClick={() => setShowProfileSetup(true)} className="shrink-0 rounded-lg border border-gray-200 px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50">更改模式</button>
          </div>
        </div>

        <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-900">PuddingClaw Backend</h2>
            {backendStatus && <span className={`rounded-full border px-3 py-1 text-xs font-medium ${statusColor[backendStatus.status] || statusColor.unknown}`}>{loading.backend && backendStatus.status !== "running" ? "启动中..." : backendStatus.status}</span>}
          </div>
          {backendStatus && (
            <div className="mb-4 space-y-2 text-sm">
              <div className="flex justify-between"><span className="text-gray-600">API 地址</span><span className="font-mono text-gray-900">{backendStatus.url}</span></div>
              {backendStatus.error && <div className="mt-2 text-xs text-red-600">{backendStatus.error}</div>}
            </div>
          )}
          <button onClick={() => void handleStopBackend()} disabled={loading.backend || backendStatus?.status === "stopped"} className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40">停止 Backend</button>
        </div>

        <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-900">可选 Docker 基础设施</h2>
            {infraStatus && <span className={`rounded-full border px-3 py-1 text-xs font-medium ${statusColor[infraStatus.status] || statusColor.unknown}`}>{infraStatus.status}</span>}
          </div>
          {infraStatus && (
            <div className="mb-4 space-y-2 text-sm">
              <div className="flex justify-between"><span className="text-gray-600">Docker Desktop</span><span className={infraStatus.docker ? "text-green-600" : "text-gray-500"}>{infraStatus.docker ? "运行中" : "未运行"}</span></div>
              <div className="flex justify-between"><span className="text-gray-600">PostgreSQL</span><span>{infraServiceStateLabel[infraStatus.postgres] || infraStatus.postgres}</span></div>
              <div className="flex justify-between"><span className="text-gray-600">Milvus</span><span>{infraServiceStateLabel[infraStatus.milvus] || infraStatus.milvus}</span></div>
              {infraStatus.error && <div className="mt-2 text-xs text-gray-500">{infraStatus.error}</div>}
            </div>
          )}
          <div className="flex gap-3">
            <button onClick={() => void handleStartInfra()} disabled={loading.infra} className="rounded-lg bg-[#002fa7] px-4 py-2 text-sm font-medium text-white hover:bg-[#001f7a] disabled:opacity-40">{loading.infra ? "处理中..." : "启动 Infra"}</button>
            <button onClick={() => void handleStopInfra()} disabled={loading.infra} className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40">停止 Infra</button>
          </div>
        </div>

        <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
          <button onClick={() => router.push("/")} disabled={backendStatus?.status !== "running"} className="w-full rounded-lg bg-[#002fa7] px-4 py-3 text-sm font-medium text-white hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-40">进入 PuddingClaw</button>
          {backendStatus?.status !== "running" && <p className="mt-2 text-center text-xs text-gray-500">等待 Backend 启动完成...</p>}
        </div>
      </div>
    </div>
  );
}
