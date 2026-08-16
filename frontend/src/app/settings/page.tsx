"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  Bot,
  Database,
  FileText,
  Brain,
  Save,
  Loader2,
  CheckCircle2,
  XCircle,
  Eye,
  EyeOff,
  Zap,
  Activity,
  Network,
  Route,
  ShieldCheck,
  ExternalLink,
  Braces,
  RefreshCw,
  FolderOpen,
  Box,
  Target,
  X,
  Search,
  Filter,
  Plus,
  ChevronDown,
  ChevronUp,
  KeyRound,
  RotateCcw,
  Globe2,
  Copy,
} from "lucide-react";
import {
  getSettings,
  updateSettings,
  resetKnowledgeVectorCollections,
  testDatabaseConnection,
  getCapabilities,
  getProviders,
  revealProviderCredential,
  updateProvider,
  bindProviderModel,
  discoverProviderModels,
  testProviderConnection,
  addProviderModel,
  type Capabilities,
  type SubAgentItem,
  type ProviderRegistry,
  type ProviderService,
  type ProviderCapability,
  type ProviderModelCategory,
} from "@/lib/settingsApi";
import { useApp } from "@/lib/store";
import {
  getLlmWikiWorkspaceStatus,
  initializeLlmWikiGbrain,
  type LlmWikiWorkspaceStatus,
} from "@/lib/api";
import MemoryEditor from "@/components/settings/MemoryEditor";
import CapabilitiesStatus from "@/components/settings/CapabilitiesStatus";
import WorkerAccessKeysPanel from "@/components/settings/WorkerAccessKeysPanel";
import SettingsAnchorLayout, { type SettingsAnchorSection } from "@/components/settings/SettingsAnchorLayout";
import SettingsNavigation, { SETTINGS_CATEGORIES, settingsCategoryEnabled, type SettingsCategory } from "@/components/settings/SettingsNavigation";
import { useRuntimeProfile } from "@/lib/useRuntimeProfile";
import Navbar from "@/components/layout/Navbar";
import Link from "next/link";
import deepseekLogo from "@lobehub/icons-static-svg/icons/deepseek-color.svg";
import bailianLogo from "@lobehub/icons-static-svg/icons/bailian-color.svg";
import moonshotLogo from "@lobehub/icons-static-svg/icons/moonshot.svg";
import siliconFlowLogo from "@lobehub/icons-static-svg/icons/siliconcloud-color.svg";

type SubAgentConfigMap = Record<string, Omit<SubAgentItem, "name">>;
type PendingModelCategoryEdit = {
  providerId: string;
  endpointId: string;
  name: string;
  capability?: ProviderCapability;
};

type HarnessSection = {
  id: string;
  label: string;
  description: string;
  icon: React.ElementType;
  disabled?: boolean;
};

const HARNESS_SECTIONS: HarnessSection[] = [
  { id: "subagent", label: "SubAgent", description: "子代理注册与状态", icon: Bot },
  { id: "context", label: "上下文工程", description: "摘要与工具上下文压缩", icon: Brain },
  { id: "prompt-cache", label: "Prompt 缓存", description: "稳定系统、消息与工具前缀", icon: Braces },
  { id: "completion", label: "Goal 与验收", description: "Goal Run Rubric 与执行预算", icon: Target },
  { id: "sandbox", label: "终端执行", description: "宿主执行与内核沙箱", icon: Box },
  { id: "runtime", label: "运行保护", description: "运行保护与权限策略", icon: ShieldCheck },
];

const DATABASE_QA_SECTIONS: SettingsAnchorSection[] = [
  { id: "preview", label: "结果预览", description: "直传、摘要与读取体量", icon: Database },
  { id: "storage", label: "持久化存储", description: "落盘、保留与导出", icon: FileText },
];

const RAG_SECTIONS: SettingsAnchorSection[] = [
  { id: "recall", label: "基础召回", description: "Top-K 与相似度阈值", icon: Search },
  { id: "hybrid", label: "混合检索", description: "关键词、语义与图文融合", icon: Network },
  { id: "rerank", label: "重排", description: "候选池与 Rerank", icon: Filter },
];

const KNOWLEDGE_SECTIONS: SettingsAnchorSection[] = [
  { id: "directory", label: "本地目录", description: "知识库资产根目录", icon: FolderOpen },
  { id: "wiki", label: "LLM Wiki", description: "编译与混合检索", icon: Bot },
  { id: "gbrain", label: "GBrain", description: "模型与独立数据库", icon: Brain },
  { id: "embedding", label: "多模态 Embedding", description: "模型绑定与批量数", icon: Network },
  { id: "index", label: "检索索引", description: "向量服务与索引维护", icon: Database },
];

const SETTINGS_CATEGORY_KEY = "settings:activeCategory";
const SETTINGS_CATEGORY_DESCRIPTIONS: Record<SettingsCategory, string> = {
  ai: "统一管理模型供应商、接口、模型分类与默认工作负载。",
  database: "PuddingClaw Core 的持久化连接，与知识库和智能问数扩展开关解耦。",
  databaseQa: "控制查询结果如何进入模型、持久化、分页与导出。",
  rag: "配置知识库检索的召回、图文融合与重排策略。",
  knowledge: "管理本地知识库目录、Wiki 编译与向量索引。",
  memory: "维护全局与项目级 Agent 记忆。",
  harness: "管理 Agent 编排、上下文、执行预算与运行保护。",
  worker: "管理 Worker Access Key 与 Headless API 调用记录。",
  system: "查看核心服务、扩展能力与运行依赖状态。",
};
const DEFAULT_IMAGE_ANALYZER_PROMPT =
  "You are an image analysis specialist. When given an image, describe its contents in detail and answer any questions about it. Return your findings as concise, structured text.";

const PROVIDER_LOGOS: Record<string, string | { src: string }> = {
  deepseek: deepseekLogo,
  dashscope: bailianLogo,
  kimi: moonshotLogo,
  siliconflow: siliconFlowLogo,
};

const PROVIDER_ACCENTS: Record<string, string> = {
  deepseek: "#4d6bfe",
  dashscope: "#6757ff",
  kimi: "#e8e8e8",
  siliconflow: "#6e29f6",
};

const PROTOCOL_LABELS: Record<string, string> = {
  deepseek: "DeepSeek 接口",
  openai_compatible: "OpenAI 兼容接口",
  dashscope_multimodal_embedding: "百炼原生多模态接口",
};

function protocolLabel(protocol: string): string {
  return PROTOCOL_LABELS[protocol] || "自定义接口";
}

const MODEL_CATEGORY_OPTIONS: Array<{ id: ProviderModelCategory; label: string; description: string; capability: ProviderCapability }> = [
  { id: "llm", label: "对话模型", description: "文本对话、规划与工具调用", capability: "llm" },
  { id: "multimodal_llm", label: "视觉模型", description: "支持图片等多模态输入的对话模型", capability: "llm" },
  { id: "text_embedding", label: "文本 Embedding", description: "文本向量化与语义检索", capability: "text_embedding" },
  { id: "multimodal_embedding", label: "多模态 Embedding", description: "图片、文本等跨模态向量化", capability: "multimodal_embedding" },
  { id: "rerank", label: "Rerank", description: "召回候选相关性重排", capability: "rerank" },
];

const MODEL_CATEGORY_LABELS = Object.fromEntries(MODEL_CATEGORY_OPTIONS.map((item) => [item.id, item.label])) as Record<ProviderModelCategory, string>;

const MODEL_CATEGORY_BINDINGS: Partial<Record<ProviderModelCategory, string>> = {
  llm: "agent",
  multimodal_llm: "image_analyzer",
  text_embedding: "text_embedding",
  multimodal_embedding: "multimodal_embedding",
  rerank: "rerank",
};

type DiscoveredModelFilter = "all" | "reasoning" | "vision" | "web" | "free" | "embedding" | "rerank" | "tools";

const DISCOVERED_MODEL_FILTERS: Array<{ id: DiscoveredModelFilter; label: string }> = [
  { id: "all", label: "全部" },
  { id: "reasoning", label: "推理" },
  { id: "vision", label: "视觉" },
  { id: "web", label: "联网" },
  { id: "free", label: "免费" },
  { id: "embedding", label: "嵌入" },
  { id: "rerank", label: "重排" },
  { id: "tools", label: "工具" },
];

function modelCategories(model: { capability: ProviderCapability; categories?: ProviderModelCategory[] }): ProviderModelCategory[] {
  if (model.categories?.length) return model.categories;
  return [model.capability];
}

function suggestModelCategories(name: string, supported: ProviderCapability[]): ProviderModelCategory[] {
  const value = name.toLowerCase();
  if (supported.includes("rerank") && /(rerank|re-rank)/.test(value)) return ["rerank"];
  if (/(embedding|embed|vector)/.test(value)) {
    if (supported.includes("multimodal_embedding") && /(multimodal|vision|\bvl\b|image|video)/.test(value)) {
      return ["multimodal_embedding"];
    }
    if (supported.includes("text_embedding")) return ["text_embedding"];
  }
  if (supported.includes("llm")) {
    const categories: ProviderModelCategory[] = ["llm"];
    if (/(multimodal|vision|\bvl\b|ocr|image|video|audio|omni|qwen3[._-]?7)/.test(value)) categories.push("multimodal_llm");
    return categories;
  }
  const fallback = MODEL_CATEGORY_OPTIONS.find((option) => supported.includes(option.capability));
  return fallback ? [fallback.id] : [];
}

function inferDiscoveredFilters(name: string, categories: ProviderModelCategory[]): DiscoveredModelFilter[] {
  const value = name.toLowerCase();
  const filters: DiscoveredModelFilter[] = [];
  if (/(reason|thinking|\br1\b|\bqwq\b|\bo[1-9](?:[-_.]|$))/.test(value)) filters.push("reasoning");
  if (categories.includes("multimodal_llm") || categories.includes("multimodal_embedding")) filters.push("vision");
  if (/(search|web|online)/.test(value)) filters.push("web");
  if (/(?:^|[-_/.])free(?:[-_/.]|$)/.test(value)) filters.push("free");
  if (categories.includes("text_embedding") || categories.includes("multimodal_embedding")) filters.push("embedding");
  if (categories.includes("rerank")) filters.push("rerank");
  if (/(tool|function|coder|coding|code)/.test(value)) filters.push("tools");
  return filters;
}

function matchesDiscoveredFilter(filters: DiscoveredModelFilter[], filter: DiscoveredModelFilter): boolean {
  return filter === "all" || filters.includes(filter);
}

function discoveredModelFamily(name: string, providerName: string): string {
  if (name.includes("/")) {
    const namespace = name.split("/", 1)[0];
    const normalized = namespace.toLowerCase();
    if (normalized === "kimi" || normalized === "moonshot") return "Kimi";
    if (normalized === "minimax") return "MiniMax";
    if (normalized === "zhipu") return "GLM";
    if (normalized === "siliconflow") return "SiliconFlow";
    if (normalized === "xiaomi") return "Xiaomi";
    return namespace;
  }
  const value = name.toLowerCase();
  if (value.startsWith("qwen")) return "Qwen";
  if (value.startsWith("deepseek")) return "DeepSeek";
  if (value.startsWith("kimi") || value.startsWith("moonshot")) return "Kimi";
  if (value.startsWith("glm") || value.startsWith("zhipu")) return "GLM";
  if (value.startsWith("minimax")) return "MiniMax";
  if (/(embedding|embed)/.test(value)) return "Embedding";
  return providerName;
}

function ProviderLogo({ provider, size = "md" }: { provider: ProviderService; size?: "sm" | "md" | "lg" }) {
  const dimensions = size === "lg" ? "h-12 w-12" : size === "md" ? "h-9 w-9" : "h-6 w-6";
  const logo = PROVIDER_LOGOS[provider.id];
  const logoSrc = typeof logo === "string" ? logo : logo?.src;
  return (
    <span
      className={`${dimensions} flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-white p-1.5 shadow-[0_0_0_1px_rgba(255,255,255,0.12)]`}
      style={{ backgroundColor: provider.id === "kimi" ? "#ffffff" : undefined }}
    >
      {logoSrc ? <img src={logoSrc} alt="" className="h-full w-full object-contain" /> : <span className="text-[10px] font-bold" style={{ color: PROVIDER_ACCENTS[provider.id] || "#7c879a" }}>{provider.name.slice(0, 1)}</span>}
    </span>
  );
}

function ModelBindingSelect({
  value,
  options,
  onChange,
  emptyLabel = "尚无可用模型",
  variant = "dark",
}: {
  value: string;
  options: Array<{ id: string; label: string }>;
  onChange: (modelId: string) => void | Promise<void>;
  emptyLabel?: string;
  variant?: "dark" | "light";
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = options.find((option) => option.id === value);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={`relative ${open ? "z-40" : ""}`}>
      <button
        type="button"
        disabled={!options.length}
        onClick={() => setOpen((current) => !current)}
        className={`flex h-11 w-full items-center gap-3 rounded-xl px-3 text-left text-[12px] outline-none transition-colors disabled:cursor-not-allowed disabled:opacity-55 ${variant === "light" ? "border border-slate-200 bg-white text-slate-800 hover:border-slate-300 focus:border-[#002fa7]" : "border border-white/[0.12] bg-[#1d1d1d] text-white hover:border-white/[0.2] focus:border-[#8d9cff]"}`}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="min-w-0 flex-1 truncate">{selected?.label || emptyLabel}</span>
        <ChevronDown className={`h-4 w-4 shrink-0 transition-transform ${variant === "light" ? "text-slate-500" : "text-white/45"} ${open ? "rotate-180" : ""}`} />
      </button>
      {open && options.length > 0 && (
        <div role="listbox" className="absolute left-0 right-0 top-[calc(100%+6px)] z-50 max-h-56 overflow-y-auto rounded-xl border border-slate-200 bg-white p-1.5 shadow-xl shadow-slate-950/20">
          {options.map((option) => {
            const active = option.id === value;
            return (
              <button
                type="button"
                role="option"
                aria-selected={active}
                key={option.id}
                onClick={() => {
                  setOpen(false);
                  if (!active) void onChange(option.id);
                }}
                className={`flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left text-[12px] transition ${active ? "bg-[#002fa7]/[0.08] font-semibold text-[#002fa7]" : "text-slate-700 hover:bg-slate-100"}`}
              >
                <span className="min-w-0 flex-1 truncate">{option.label}</span>
                {active && <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function subagentItemsToConfig(items: SubAgentItem[]): SubAgentConfigMap {
  return items.reduce<SubAgentConfigMap>((acc, item, index) => {
    const name = item.name.trim() || `subagent_${index + 1}`;
    const { name: _name, ...stored } = item;
    acc[name] = stored;
    return acc;
  }, {});
}

function isSubAgentValid(item: SubAgentItem): boolean {
  return (
    item.name.trim() !== "" &&
    item.model.trim() !== "" &&
    item.description.trim() !== "" &&
    item.system_prompt.trim() !== ""
  );
}

function positiveIntOrNull(value: string): number | null {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function postgresConnectionUrl(input: {
  host: string;
  port: number;
  database: string;
  username: string;
  password: string;
}): string {
  const rawHost = input.host.trim() || "127.0.0.1";
  const host = rawHost.includes(":") && !rawHost.startsWith("[") ? `[${rawHost}]` : rawHost;
  const credentials = input.password
    ? `${encodeURIComponent(input.username)}:${encodeURIComponent(input.password)}`
    : encodeURIComponent(input.username);
  return `postgresql://${credentials}@${host}:${input.port}/${encodeURIComponent(input.database)}`;
}

export default function SettingsPage() {
  const {
    sidebarOpen,
    toggleSidebar,
    sessionId,
    setSessionId,
    setWorkspaceView,
  } = useApp();
  const [mounted, setMounted] = useState(false);
  const [selectedSubagentIndex, setSelectedSubagentIndex] = useState<number | null>(null);
  const [configModalOpen, setConfigModalOpen] = useState(false);
  useEffect(() => {
    setMounted(true);
    const params = new URLSearchParams(window.location.search);
    const categoryParam = params.get("category");
    if (SETTINGS_CATEGORIES.some((item) => item.key === categoryParam)) {
      setCategory(categoryParam as SettingsCategory);
    }
  }, []);
  const handleReturnToApp = useCallback(() => {
    let targetSessionId = sessionId;
    try {
      targetSessionId = sessionStorage.getItem("puddingclaw_session_id") || sessionId;
    } catch {
      // The in-memory Session remains the fallback when storage is unavailable.
    }
    setWorkspaceView("chat");
    if (targetSessionId !== sessionId) setSessionId(targetSessionId);
  }, [sessionId, setSessionId, setWorkspaceView]);
  const [category, setCategory] = useState<SettingsCategory>(() => {
    if (typeof window === "undefined") return "ai";
    const saved = localStorage.getItem(SETTINGS_CATEGORY_KEY);
    const valid = SETTINGS_CATEGORIES.some((c) => c.key === saved);
    return (valid ? (saved as SettingsCategory) : "ai");
  });
  const runtimeExtensions = useRuntimeProfile();
  const activeCategory = settingsCategoryEnabled(category, runtimeExtensions) ? category : "ai";
  useEffect(() => {
    if (runtimeExtensions && activeCategory !== category) setCategory(activeCategory);
  }, [activeCategory, category, runtimeExtensions]);
  const [providerRegistry, setProviderRegistry] = useState<ProviderRegistry | null>(null);
  const agentModels = providerRegistry?.providers.flatMap((provider) =>
    provider.models
      .filter((model) => model.capability === "llm")
      .map((model) => model.name)
  ) || [];
  const multimodalEmbeddingSelection = (() => {
    const modelId = providerRegistry?.bindings?.multimodal_embedding;
    if (!modelId) return null;
    for (const provider of providerRegistry.providers) {
      const model = provider.models.find((item) => item.id === modelId);
      if (model) return { provider, model };
    }
    return null;
  })();
  const [providerBusy, setProviderBusy] = useState<string | null>(null);
  const [providerUrls, setProviderUrls] = useState<Record<string, string>>({});
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({});
  const [providerRevealedKeys, setProviderRevealedKeys] = useState<Record<string, boolean>>({});
  const [providerCredentialNames, setProviderCredentialNames] = useState<Record<string, string>>({});
  const [providerAddingCredentialId, setProviderAddingCredentialId] = useState<string | null>(null);
  const [discoveredProviderModels, setDiscoveredProviderModels] = useState<Record<string, string[]>>({});
  const [providerConnectionResults, setProviderConnectionResults] = useState<Record<string, { ok: boolean; message: string }>>({});
  const providerConnectionResultTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const providerKeyInputRef = useRef<HTMLInputElement>(null);
  const [providerModelPicker, setProviderModelPicker] = useState<{ providerId: string; endpointId: string } | null>(null);
  const [providerModelSearch, setProviderModelSearch] = useState("");
  const [providerModelFilter, setProviderModelFilter] = useState<DiscoveredModelFilter>("all");
  const [providerModelPickerError, setProviderModelPickerError] = useState("");
  const [providerModelAdding, setProviderModelAdding] = useState<string | null>(null);
  const [pendingModelCategoryEdit, setPendingModelCategoryEdit] = useState<PendingModelCategoryEdit | null>(null);
  const [selectedModelCategories, setSelectedModelCategories] = useState<ProviderModelCategory[]>([]);
  const [expandedDiscoveredFamilies, setExpandedDiscoveredFamilies] = useState<Record<string, boolean>>({});
  const [providerSearch, setProviderSearch] = useState("");
  const [providerSearchEnabled, setProviderSearchEnabled] = useState(false);
  const [onlyConfiguredProviders, setOnlyConfiguredProviders] = useState(false);
  const [selectedProviderId, setSelectedProviderId] = useState<string | "defaults">("defaults");
  const [selectedEndpointId, setSelectedEndpointId] = useState("");
  const [showProviderKey, setShowProviderKey] = useState(false);
  const [expandedModelGroups, setExpandedModelGroups] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);

  useEffect(() => {
    localStorage.setItem(SETTINGS_CATEGORY_KEY, category);
  }, [category]);

  // RAG form state
  const [ragTopK, setRagTopK] = useState(10);
  const [ragThreshold, setRagThreshold] = useState(0.5);
  const [ragTextVectorWeight, setRagTextVectorWeight] = useState(0.7);
  const [ragImageVectorWeight, setRagImageVectorWeight] = useState(0.4);
  const [ragHybridCandidateTopK, setRagHybridCandidateTopK] = useState(30);
  const [ragRerankEnabled, setRagRerankEnabled] = useState(true);
  const [ragRerankCandidateTopK, setRagRerankCandidateTopK] = useState(50);
  const ragBm25Weight = Math.max(0, Math.min(1, 1 - ragTextVectorWeight));
  const ragTextGroupWeight = Math.max(0, Math.min(1, 1 - ragImageVectorWeight));

  // Smart Database Q&A
  const [dbQaFullRowsTokenBudget, setDbQaFullRowsTokenBudget] = useState("10000");
  const [dbQaPreviewRowsTokenBudget, setDbQaPreviewRowsTokenBudget] = useState("3000");
  const [dbQaProfileTokenBudget, setDbQaProfileTokenBudget] = useState("3000");
  const [dbQaFullRowsHardRowCap, setDbQaFullRowsHardRowCap] = useState("200");
  const [dbQaFullRowsHardColumnCap, setDbQaFullRowsHardColumnCap] = useState("20");
  const [dbQaMaxCellCharsForLlm, setDbQaMaxCellCharsForLlm] = useState("500");
  const [dbQaResultMaterializationRowCap, setDbQaResultMaterializationRowCap] = useState("99999");
  const [dbQaQueryTimeoutSeconds, setDbQaQueryTimeoutSeconds] = useState("30");
  const [dbQaSqlGenerationTimeoutSeconds, setDbQaSqlGenerationTimeoutSeconds] = useState("210");
  const [dbQaResultStoreEnabled, setDbQaResultStoreEnabled] = useState(true);
  const [dbQaResultStoreTtlHours, setDbQaResultStoreTtlHours] = useState("168");
  const [dbQaDefaultPageSize, setDbQaDefaultPageSize] = useState("100");
  const [dbQaMaxPageSize, setDbQaMaxPageSize] = useState("500");
  const [dbQaExportEnabled, setDbQaExportEnabled] = useState(false);
  const [dbQaProfileEnabled, setDbQaProfileEnabled] = useState(true);
  const [dbQaAgentSqlFallbackEnabled, setDbQaAgentSqlFallbackEnabled] = useState(true);

  // Core database
  const [databaseMode, setDatabaseMode] = useState<"sqlite" | "bundled" | "external">("sqlite");
  const [databaseHost, setDatabaseHost] = useState("127.0.0.1");
  const [databasePort, setDatabasePort] = useState("5432");
  const [databaseName, setDatabaseName] = useState("puddingclaw");
  const [databaseUsername, setDatabaseUsername] = useState("pet");
  const [databasePassword, setDatabasePassword] = useState("");
  const [databaseConfiguredBy, setDatabaseConfiguredBy] = useState("default");
  const [databaseSource, setDatabaseSource] = useState("config");
  const [databaseCatalogPath, setDatabaseCatalogPath] = useState("$PUDDINGCLAW_HOME/db/catalog.sqlite3");
  const [databaseEnvOverride, setDatabaseEnvOverride] = useState(false);
  // 当前生效的存储提供方（sqlite / postgresql），用于切换前的二次确认；
  // 与表单中的 databaseMode 区分：后者是用户尚未保存的选择。
  const [databaseAppliedProvider, setDatabaseAppliedProvider] = useState<"sqlite" | "postgresql">("sqlite");
  const [databaseTesting, setDatabaseTesting] = useState(false);
  const [databaseTestResult, setDatabaseTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [knowledgeRootDir, setKnowledgeRootDir] = useState("");
  const [knowledgeConfiguredBy, setKnowledgeConfiguredBy] = useState("default");
  const [knowledgeEnvOverride, setKnowledgeEnvOverride] = useState(false);
  const [wikiCompilerModelId, setWikiCompilerModelId] = useState("");
  const [wikiHybridEnabled, setWikiHybridEnabled] = useState(false);
  const [wikiHybridSaving, setWikiHybridSaving] = useState(false);
  const [wikiGbrainEmbeddingModelId, setWikiGbrainEmbeddingModelId] = useState("");
  const [wikiGbrainThinkModelId, setWikiGbrainThinkModelId] = useState("");
  const [gbrainWorkspace, setGbrainWorkspace] = useState<LlmWikiWorkspaceStatus | null>(null);
  const [gbrainDatabaseHost, setGbrainDatabaseHost] = useState("127.0.0.1");
  const [gbrainDatabasePort, setGbrainDatabasePort] = useState("5432");
  const [gbrainDatabaseName, setGbrainDatabaseName] = useState("llm_wiki");
  const [gbrainDatabaseUsername, setGbrainDatabaseUsername] = useState("pet");
  const [gbrainDatabasePassword, setGbrainDatabasePassword] = useState("");
  const [gbrainDatabaseTesting, setGbrainDatabaseTesting] = useState(false);
  const [gbrainInitializing, setGbrainInitializing] = useState(false);
  const [gbrainDatabaseTestResult, setGbrainDatabaseTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  useEffect(() => {
    if (!gbrainDatabaseTestResult?.ok) return;
    const timer = window.setTimeout(() => setGbrainDatabaseTestResult(null), 3000);
    return () => window.clearTimeout(timer);
  }, [gbrainDatabaseTestResult]);
  const [mmBatchSize, setMmBatchSize] = useState("10");
  const [kbIndexEnabled, setKbIndexEnabled] = useState(true);
  const [kbVectorStore, setKbVectorStore] = useState("milvus");
  const [kbMilvusUri, setKbMilvusUri] = useState("http://localhost:19530");
  const [kbTextCollection, setKbTextCollection] = useState("puddingclaw_knowledge_text");
  const [kbImageCollection, setKbImageCollection] = useState("puddingclaw_knowledge_image");

  // Harness context engineering (DeepAgents only)
  const [contextSummaryModelId, setContextSummaryModelId] = useState("");
  const [contextSummaryTriggerTokens, setContextSummaryTriggerTokens] = useState("200000");
  const [contextSummaryKeepTokens, setContextSummaryKeepTokens] = useState("64000");
  const [toolContextEnabled, setToolContextEnabled] = useState(true);
  const [immediateToolCompactionEnabled, setImmediateToolCompactionEnabled] = useState(false);
  const [singleToolTriggerTokens, setSingleToolTriggerTokens] = useState("8000");
  const [backgroundMinResultTokens, setBackgroundMinResultTokens] = useState("1000");
  const [retainToolContextTokens, setRetainToolContextTokens] = useState("32000");

  // Harness prompt-cache stability
  const [tracePartDiagnostics, setTracePartDiagnostics] = useState(true);
  const [orderedSystemSections, setOrderedSystemSections] = useState(true);
  const [tailRoutingMessage, setTailRoutingMessage] = useState(true);
  const [deterministicSessionProjection, setDeterministicSessionProjection] = useState(true);
  const [stableToolSchema, setStableToolSchema] = useState(false);

  // Harness runtime policy
  const [modelCallLimitEnabled, setModelCallLimitEnabled] = useState(true);
  const [modelCallRunLimit, setModelCallRunLimit] = useState("50");
  const [modelCallThreadLimit, setModelCallThreadLimit] = useState("");
  const [modelCallExitBehavior, setModelCallExitBehavior] = useState<"end" | "error">("end");
  const [rubricEnabled, setRubricEnabled] = useState(false);
  const [rubricMaxIterations, setRubricMaxIterations] = useState("2");
  const [rubricMaxStagnantRepairs, setRubricMaxStagnantRepairs] = useState("2");
  const [customRubricRulesEnabled, setCustomRubricRulesEnabled] = useState(false);
  const [customRubricRules, setCustomRubricRules] = useState<Array<{
    id: string;
    enabled: boolean;
    statement: string;
    required: boolean;
    verifier: "analytics" | "llm_grader";
  }>>([]);
  const [goalsEnabled, setGoalsEnabled] = useState(true);
  const [goalMaxRounds, setGoalMaxRounds] = useState("8");
  const [executionMode, setExecutionMode] = useState<"spawn" | "kernel">("spawn");

  // Harness left-right anchor layout
  const [harnessFilter, setHarnessFilter] = useState("");
  const [activeHarnessSection, setActiveHarnessSection] = useState("subagent");

  // SubAgent / Harness
  const [subagentItems, setSubagentItems] = useState<SubAgentItem[]>([]);
  const [refreshingModels, setRefreshingModels] = useState(false);

  const makeDefaultSubAgentItem = useCallback((models: string[]): SubAgentItem => {
    return {
      enabled: true,
      name: "image_analyzer",
      model: models[0] || "",
      description: "Analyze image inputs and answer questions about them.",
      route_trigger: "image_input",
      tools: { mode: "inherit" },
      skills: { mode: "inherit", paths: [] },
      system_prompt: DEFAULT_IMAGE_ANALYZER_PROMPT,
    };
  }, []);

  const updateSubAgentItem = useCallback((index: number, updater: (item: SubAgentItem) => SubAgentItem) => {
    setSubagentItems((prev) => prev.map((item, i) => (i === index ? updater(item) : item)));
  }, []);

  // Load settings and capabilities on mount
  useEffect(() => {
    if (!runtimeExtensions) return;
    // Infrastructure probes can wait on several network timeouts. They update
    // status indicators in the background and must not block the settings form.
    getCapabilities()
      .then(setCapabilities)
      .catch(() => {});

    getSettings()
      .then((s) => {
        setProviderRegistry(s.provider_registry || null);
        // Populate RAG fields
        setRagTopK(s.rag.top_k);
        setRagThreshold(s.rag.similarity_threshold);
        const textWeight = s.rag.hybrid?.text_vector_weight ?? 0.7;
        const keywordWeight = s.rag.hybrid?.bm25_weight ?? 0.3;
        const textMixTotal = textWeight + keywordWeight;
        setRagTextVectorWeight(textMixTotal > 0 ? textWeight / textMixTotal : 0.7);
        setRagImageVectorWeight(s.rag.hybrid?.image_vector_weight ?? 0.4);
        setRagHybridCandidateTopK(s.rag.hybrid?.candidate_top_k ?? 30);
        setRagRerankEnabled(s.rag.rerank?.enabled ?? true);
        setRagRerankCandidateTopK(s.rag.rerank?.candidate_top_k ?? 50);
        const databaseQa = s.analytics?.database_qa;
        setDbQaFullRowsTokenBudget(String(databaseQa?.full_rows_token_budget ?? 10000));
        setDbQaPreviewRowsTokenBudget(String(databaseQa?.preview_rows_token_budget ?? 3000));
        setDbQaProfileTokenBudget(String(databaseQa?.profile_token_budget ?? 3000));
        setDbQaFullRowsHardRowCap(String(databaseQa?.full_rows_hard_row_cap ?? 200));
        setDbQaFullRowsHardColumnCap(String(databaseQa?.full_rows_hard_column_cap ?? 20));
        setDbQaMaxCellCharsForLlm(String(databaseQa?.max_cell_chars_for_llm ?? 500));
        setDbQaResultMaterializationRowCap(String(databaseQa?.result_materialization_row_cap ?? 99999));
        setDbQaQueryTimeoutSeconds(String(Math.max(1, Math.round((databaseQa?.query_timeout_ms ?? 30000) / 1000))));
        setDbQaSqlGenerationTimeoutSeconds(String(Math.max(30, Math.round((databaseQa?.sql_generation_timeout_ms ?? 210000) / 1000))));
        setDbQaResultStoreEnabled(databaseQa?.result_store_enabled ?? true);
        setDbQaResultStoreTtlHours(String(databaseQa?.result_store_ttl_hours ?? 168));
        setDbQaDefaultPageSize(String(databaseQa?.default_page_size ?? 100));
        setDbQaMaxPageSize(String(databaseQa?.max_page_size ?? 500));
        setDbQaExportEnabled(databaseQa?.export_enabled ?? true);
        setDbQaProfileEnabled(databaseQa?.profile_enabled ?? true);
        setDbQaAgentSqlFallbackEnabled(databaseQa?.database_agent_sql_fallback_enabled ?? true);
        // Core database. "bundled" is deployment provenance, not a third
        // database type users should select in the standalone settings UI.
        // Newer backends report a `provider` field (sqlite / postgresql);
        // prefer it over the legacy mode when present.
        const databaseEnvironmentOverride = Boolean(s.database?.environment_override);
        const databaseProvider = s.database?.provider;
        const loadedDatabaseMode = databaseProvider === "sqlite"
          ? "sqlite"
          : databaseProvider === "postgresql"
            ? "external"
            : s.database?.mode === "sqlite"
              ? "sqlite"
              : s.database?.mode === "external"
                ? "external"
                : "bundled";
        setDatabaseMode(databaseEnvironmentOverride ? loadedDatabaseMode : loadedDatabaseMode === "sqlite" ? "sqlite" : "external");
        setDatabaseAppliedProvider(loadedDatabaseMode === "sqlite" ? "sqlite" : "postgresql");
        setDatabaseHost(s.database?.host || "127.0.0.1");
        setDatabasePort(String(s.database?.port || 5432));
        setDatabaseName(s.database?.database || "puddingclaw");
        setDatabaseUsername(s.database?.username || "puddingclaw");
        // Passwords are write-only. An empty field means "keep the stored
        // credential" when the rest of the settings form is saved.
        setDatabasePassword("");
        setDatabaseConfiguredBy(s.database?.configured_by || "default");
        setDatabaseSource(s.database?.source || "config");
        setDatabaseCatalogPath(s.database?.catalog_path || "$PUDDINGCLAW_HOME/db/catalog.sqlite3");
        setDatabaseEnvOverride(databaseEnvironmentOverride);
        setKnowledgeRootDir(s.knowledge?.root_dir || "");
        setKnowledgeConfiguredBy(s.knowledge?.configured_by || "default");
        setKnowledgeEnvOverride(Boolean(s.knowledge?.environment_override));
        setWikiCompilerModelId(s.knowledge?.llm_wiki?.compiler_agent?.model_id || "");
        setWikiHybridEnabled(s.knowledge?.llm_wiki?.retrieval?.hybrid_enabled ?? true);
        setWikiGbrainEmbeddingModelId(s.knowledge?.llm_wiki?.gbrain?.embedding_model_id || "");
        setWikiGbrainThinkModelId(s.knowledge?.llm_wiki?.gbrain?.think_model_id || "");
        setGbrainDatabaseHost(s.database?.host || "127.0.0.1");
        setGbrainDatabasePort(String(s.database?.port || 5432));
        setGbrainDatabaseName("llm_wiki");
        setGbrainDatabaseUsername(s.database?.username || "puddingclaw");
        setGbrainDatabasePassword(s.database?.password || "");
        if (runtimeExtensions?.knowledge) getLlmWikiWorkspaceStatus()
          .then((workspace) => {
            setGbrainWorkspace(workspace);
            const postgres = workspace.gbrain.postgres;
            if (postgres?.configured) {
              setGbrainDatabaseHost(postgres.host || "127.0.0.1");
              setGbrainDatabasePort(String(postgres.port || 5432));
              setGbrainDatabaseName(postgres.database || "llm_wiki");
              setGbrainDatabaseUsername(postgres.username || "puddingclaw");
              setGbrainDatabasePassword("");
            }
          })
          .catch(() => {});
        setMmBatchSize(String(s.knowledge?.multimodal_index?.embedding_batch_size || 10));
        setKbIndexEnabled(s.knowledge?.multimodal_index?.enabled ?? true);
        setKbVectorStore(s.knowledge?.multimodal_index?.vector_store || "milvus");
        setKbMilvusUri(s.knowledge?.multimodal_index?.milvus_uri || "http://localhost:19530");
        setKbTextCollection(s.knowledge?.multimodal_index?.text_collection || "puddingclaw_knowledge_text");
        setKbImageCollection(s.knowledge?.multimodal_index?.image_collection || "puddingclaw_knowledge_image");
        setContextSummaryModelId(s.compression.deepagents?.summarization?.model_id || "");
        setContextSummaryTriggerTokens(
          String(s.compression.deepagents?.summarization?.trigger_tokens ?? 272000)
        );
        setContextSummaryKeepTokens(
          String(s.compression.deepagents?.summarization?.keep_tokens ?? 64000)
        );
        setToolContextEnabled(s.compression.deepagents?.tool_context?.enabled ?? true);
        setImmediateToolCompactionEnabled(
          s.compression.deepagents?.tool_context?.immediate_compaction_enabled ?? false
        );
        setSingleToolTriggerTokens(
          String(s.compression.deepagents?.tool_context?.single_tool_trigger_tokens ?? 8000)
        );
        setBackgroundMinResultTokens(
          String(s.compression.deepagents?.tool_context?.background_min_result_tokens ?? 1000)
        );
        setRetainToolContextTokens(
          String(s.compression.deepagents?.tool_context?.retain_tool_context_tokens ?? 32000)
        );
        const promptCache = s.harness?.prompt_cache;
        setTracePartDiagnostics(promptCache?.trace_part_diagnostics ?? true);
        setOrderedSystemSections(promptCache?.ordered_system_sections ?? true);
        setTailRoutingMessage(promptCache?.tail_routing_message ?? true);
        setDeterministicSessionProjection(promptCache?.deterministic_session_projection ?? true);
        setStableToolSchema(promptCache?.stable_tool_schema ?? false);
        // Harness runtime policy
        const modelLimit = s.harness?.model_call_limit;
        setModelCallLimitEnabled(modelLimit?.enabled ?? true);
        setModelCallRunLimit(modelLimit?.run_limit ? String(modelLimit.run_limit) : "50");
        setModelCallThreadLimit(modelLimit?.thread_limit ? String(modelLimit.thread_limit) : "");
        setModelCallExitBehavior(modelLimit?.exit_behavior === "error" ? "error" : "end");
        const rubric = s.harness?.completion?.rubric;
        setRubricEnabled(rubric?.enabled ?? false);
        setRubricMaxIterations(String(rubric?.max_iterations ?? 3));
        setRubricMaxStagnantRepairs(String(rubric?.max_stagnant_repairs ?? 2));
        setCustomRubricRulesEnabled(rubric?.custom_rules_enabled ?? false);
        setCustomRubricRules(
          Array.isArray(rubric?.custom_rules)
            ? rubric.custom_rules.map((rule) => ({
                ...rule,
                verifier: rule.verifier === "analytics" ? "analytics" : "llm_grader",
              }))
            : []
        );
        setGoalsEnabled(s.harness?.goals?.enabled ?? true);
        setGoalMaxRounds(String(s.harness?.goals?.max_rounds ?? 8));
        const terminal = s.harness?.terminal;
        setExecutionMode(terminal?.execution_mode === "kernel" ? "kernel" : "spawn");
        // SubAgent
        const items = s.subagents?.items || s.subagent?.items;
        if (Array.isArray(items) && items.length > 0) {
          setSubagentItems(items);
          setSelectedSubagentIndex(0);
        } else {
          const registryModels = (s.provider_registry?.providers || []).flatMap((provider) =>
            provider.models
              .filter((model) => model.capability === "llm")
              .map((model) => model.name)
          );
          setSubagentItems([makeDefaultSubAgentItem(registryModels)]);
          setSelectedSubagentIndex(0);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [makeDefaultSubAgentItem, runtimeExtensions]);

  const showToast = useCallback((type: "success" | "error", message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 3000);
  }, []);

  const refreshProviders = useCallback(async () => {
    const fresh = await getProviders();
    setProviderRegistry(fresh);
  }, []);

  useEffect(() => {
    if (!providerRegistry) return;
    setProviderUrls(Object.fromEntries(providerRegistry.providers.flatMap((provider) => provider.endpoints.map((endpoint) => [`${provider.id}:${endpoint.id}`, endpoint.base_url]))));
  }, [providerRegistry]);

  useEffect(() => {
    if (!providerRegistry) return;
    const provider = providerRegistry.providers.find((item) => item.id === selectedProviderId);
    if (!provider && selectedProviderId !== "defaults") {
      setSelectedProviderId(providerRegistry.providers[0]?.id || "defaults");
      return;
    }
    if (provider && !provider.endpoints.some((endpoint) => endpoint.id === selectedEndpointId)) {
      setSelectedEndpointId(provider.endpoints[0]?.id || "");
      setShowProviderKey(false);
    }
  }, [providerRegistry, selectedEndpointId, selectedProviderId]);

  const handleProviderSave = useCallback(async (provider: ProviderService, endpointId: string, baseUrl: string | undefined, apiKey: string, credentialName = "default") => {
    const keyToSave = apiKey.trim();
    const keyName = credentialName.trim();
    if (baseUrl === undefined && !keyToSave) {
      showToast("error", "请输入要保存的 API 密钥");
      return;
    }
    if (baseUrl === undefined && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(keyName)) {
      showToast("error", "Key 名称仅支持字母、数字、点、下划线和短横线");
      return;
    }
    setProviderBusy(`${provider.id}:${endpointId}`);
    try {
      const fresh = await updateProvider(provider.id, {
        enabled: provider.enabled,
        endpoints: [{ id: endpointId, ...(baseUrl !== undefined ? { base_url: baseUrl } : {}) }],
        ...(keyToSave ? { credentials: [{ name: keyName, value: keyToSave }] } : {}),
      });
      const savedCredential = fresh.providers
        .find((item) => item.id === provider.id)
        ?.api_keys.find((item) => item.name === keyName);
      const savedMaskMatches = keyToSave.length <= 8
        || savedCredential?.api_key_masked.endsWith(keyToSave.slice(-4));
      if (keyToSave && (
        !savedCredential?.credential_configured
        || savedCredential.credential_source !== "local_file"
        || !savedMaskMatches
      )) {
        throw new Error("新密钥未写入本地凭证存储，保存已被判定为失败");
      }
      setProviderRegistry(fresh);
      if (keyToSave) {
        setProviderCredentialNames((current) => ({ ...current, [provider.id]: keyName }));
        setProviderAddingCredentialId(null);
      }
      setProviderKeys((current) => {
        const next = { ...current };
        delete next[`${provider.id}:${keyName}`];
        delete next[`${provider.id}:__new__`];
        return next;
      });
      setProviderRevealedKeys((current) => {
        const next = { ...current };
        delete next[`${provider.id}:${keyName}`];
        return next;
      });
      showToast("success", keyToSave ? `${provider.name} · ${keyName} 已保存` : `${provider.name} API 地址已保存`);
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "保存 Provider 失败");
    } finally {
      setProviderBusy(null);
    }
  }, [showToast]);

  const handleProviderKeyVisibility = useCallback(async (
    provider: ProviderService,
    credentialName: string,
    credentialConfigured: boolean,
  ) => {
    const key = `${provider.id}:${credentialName}`;
    if (showProviderKey) {
      setShowProviderKey(false);
      if (providerRevealedKeys[key]) {
        setProviderKeys((current) => {
          const next = { ...current };
          delete next[key];
          return next;
        });
        setProviderRevealedKeys((current) => {
          const next = { ...current };
          delete next[key];
          return next;
        });
      }
      return;
    }
    if (providerKeys[key] || !credentialConfigured) {
      setShowProviderKey(true);
      return;
    }
    setProviderBusy(`${key}:reveal`);
    try {
      const value = await revealProviderCredential(provider.id, credentialName);
      setProviderKeys((current) => ({ ...current, [key]: value }));
      setProviderRevealedKeys((current) => ({ ...current, [key]: true }));
      setShowProviderKey(true);
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "读取 Provider 密钥失败");
    } finally {
      setProviderBusy(null);
    }
  }, [providerKeys, providerRevealedKeys, showProviderKey, showToast]);

  const handleBindProvider = useCallback(async (binding: string, modelId: string) => {
    try {
      await bindProviderModel(binding, modelId);
      await refreshProviders();
      window.dispatchEvent(new CustomEvent("puddingclaw:provider-bindings-changed", {
        detail: { binding, modelId },
      }));
      showToast("success", "默认模型已更新");
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "更新默认模型失败");
    }
  }, [refreshProviders, showToast]);

  const handleDiscoverProvider = useCallback(async (provider: ProviderService, endpointId: string) => {
    setProviderModelPickerError("");
    setProviderBusy(`${provider.id}:${endpointId}:discover`);
    try {
      const models = await discoverProviderModels(provider.id, endpointId);
      setDiscoveredProviderModels((current) => ({ ...current, [`${provider.id}:${endpointId}`]: models.map((model) => model.name) }));
      if (!models.length) setProviderModelPickerError("Provider 未返回模型列表");
    } catch (err) {
      setProviderModelPickerError(err instanceof Error ? err.message : "获取模型列表失败");
    } finally {
      setProviderBusy(null);
    }
  }, []);

  const handleOpenProviderModelPicker = useCallback((provider: ProviderService, endpointId: string) => {
    setProviderModelPicker({ providerId: provider.id, endpointId });
    setProviderModelSearch("");
    setProviderModelFilter("all");
    setProviderModelPickerError("");
    void handleDiscoverProvider(provider, endpointId);
  }, [handleDiscoverProvider]);

  const showProviderConnectionResult = useCallback((key: string, result: { ok: boolean; message: string }) => {
    const previousTimer = providerConnectionResultTimers.current[key];
    if (previousTimer) clearTimeout(previousTimer);

    setProviderConnectionResults((current) => ({ ...current, [key]: result }));
    providerConnectionResultTimers.current[key] = setTimeout(() => {
      setProviderConnectionResults((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      delete providerConnectionResultTimers.current[key];
    }, 5_000);
  }, []);

  const handleTestProviderConnection = useCallback(async (provider: ProviderService, endpointId: string) => {
    const key = `${provider.id}:${endpointId}`;
    setProviderBusy(`${key}:test`);
    const previousTimer = providerConnectionResultTimers.current[key];
    if (previousTimer) {
      clearTimeout(previousTimer);
      delete providerConnectionResultTimers.current[key];
    }
    setProviderConnectionResults((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    try {
      const result = await testProviderConnection(provider.id, endpointId, {
        base_url: providerUrls[key] || "",
        api_key: providerKeyInputRef.current?.value.trim() || providerKeys[`${provider.id}:${providerCredentialNames[provider.id] || "default"}`] || "",
        credential_name: providerCredentialNames[provider.id] || "default",
      });
      const message = `连接成功 · HTTP ${result.status_code} · ${result.latency_ms}ms`;
      showProviderConnectionResult(key, { ok: true, message });
    } catch (err) {
      const message = err instanceof Error ? err.message : "连通性检测失败";
      showProviderConnectionResult(key, { ok: false, message });
    } finally {
      setProviderBusy(null);
    }
  }, [providerCredentialNames, providerKeys, providerUrls, showProviderConnectionResult]);

  useEffect(() => () => {
    Object.values(providerConnectionResultTimers.current).forEach((timer) => clearTimeout(timer));
  }, []);

  const openModelCategoryEditor = useCallback((
    provider: ProviderService,
    endpointId: string,
    name: string,
    existing?: { capability: ProviderCapability; categories?: ProviderModelCategory[] },
  ) => {
    setPendingModelCategoryEdit({ providerId: provider.id, endpointId, name, capability: existing?.capability });
    const endpoint = provider.endpoints.find((item) => item.id === endpointId);
    setSelectedModelCategories(existing ? modelCategories(existing) : suggestModelCategories(name, endpoint?.capabilities || []));
  }, []);

  const handleSaveModelCategories = useCallback(async () => {
    if (!pendingModelCategoryEdit || selectedModelCategories.length === 0) {
      showToast("error", "请至少选择一个模型分类");
      return;
    }
    const selectedOptions = MODEL_CATEGORY_OPTIONS.filter((option) => selectedModelCategories.includes(option.id));
    const capability = selectedOptions[0]?.capability;
    if (!capability || selectedOptions.some((option) => option.capability !== capability)) {
      showToast("error", "一个模型的分类必须使用同一种调用协议");
      return;
    }
    const provider = providerRegistry?.providers.find((item) => item.id === pendingModelCategoryEdit.providerId);
    const endpoint = provider?.endpoints.find((item) => item.id === pendingModelCategoryEdit.endpointId);
    if (!provider || !endpoint?.capabilities.includes(capability)) {
      showToast("error", "该 Endpoint 不支持所选模型分类");
      return;
    }
    setProviderModelAdding(pendingModelCategoryEdit.name);
    try {
      await addProviderModel(provider.id, {
        endpoint_id: endpoint.id,
        capability,
        name: pendingModelCategoryEdit.name,
        categories: selectedModelCategories,
      });
      await refreshProviders();
      showToast("success", `${pendingModelCategoryEdit.name} 的分类已保存`);
      setPendingModelCategoryEdit(null);
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "保存模型分类失败");
    } finally {
      setProviderModelAdding(null);
    }
  }, [pendingModelCategoryEdit, providerRegistry, refreshProviders, selectedModelCategories, showToast]);

  useEffect(() => {
    if (!providerModelPicker) return;
    const previousOverflow = document.body.style.overflow;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (pendingModelCategoryEdit) setPendingModelCategoryEdit(null);
      else setProviderModelPicker(null);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [pendingModelCategoryEdit, providerModelPicker]);

  const handleAddManualProviderModel = useCallback((provider: ProviderService, endpointId: string) => {
    const name = window.prompt("输入模型 ID，例如 qwen-plus");
    if (!name?.trim()) return;
    openModelCategoryEditor(provider, endpointId, name.trim());
  }, [openModelCategoryEditor]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const summaryTrigger = positiveIntOrNull(contextSummaryTriggerTokens) ?? 200000;
      const summaryKeep = positiveIntOrNull(contextSummaryKeepTokens) ?? 64000;
      const singleToolTrigger = positiveIntOrNull(singleToolTriggerTokens) ?? 8000;
      const backgroundMinimum = positiveIntOrNull(backgroundMinResultTokens) ?? 1000;
      const retainedToolTokens = positiveIntOrNull(retainToolContextTokens) ?? 32000;
      if (summaryTrigger < 10000 || summaryTrigger > 1000000) {
        throw new Error("全局摘要阈值必须在 10,000 到 1,000,000 tokens 之间");
      }
      if (summaryKeep < 1000 || summaryKeep >= summaryTrigger) {
        throw new Error("摘要保留预算必须在 1,000 tokens 到全局摘要阈值之间");
      }
      if (singleToolTrigger < 1000 || singleToolTrigger > 20000) {
        throw new Error("执行中单条工具阈值必须在 1,000 到 20,000 tokens 之间");
      }
      if (backgroundMinimum < 100 || backgroundMinimum > 100000) {
        throw new Error("静默压缩单条下限必须在 100 到 100,000 tokens 之间");
      }
      if (retainedToolTokens < 1000 || retainedToolTokens > 500000) {
        throw new Error("工具上下文保留预算必须在 1,000 到 500,000 tokens 之间");
      }
      const updates: Record<string, unknown> = {
        ...(runtimeExtensions?.knowledge ? { rag: {
          top_k: ragTopK,
          similarity_threshold: ragThreshold,
          hybrid: {
            enabled: true,
            mode: "reciprocal_rerank",
            text_vector_weight: ragTextVectorWeight,
            image_vector_weight: ragImageVectorWeight,
            bm25_weight: ragBm25Weight,
            candidate_top_k: ragHybridCandidateTopK,
          },
          rerank: {
            enabled: ragRerankEnabled,
            provider: "dashscope",
            model: "qwen3-vl-rerank",
            top_n: ragTopK,
            candidate_top_k: ragRerankCandidateTopK,
          },
        }} : {}),
        ...(runtimeExtensions?.analytics ? { analytics: {
          database_qa: {
            full_rows_token_budget: positiveIntOrNull(dbQaFullRowsTokenBudget) ?? 10000,
            preview_rows_token_budget: positiveIntOrNull(dbQaPreviewRowsTokenBudget) ?? 3000,
            profile_token_budget: positiveIntOrNull(dbQaProfileTokenBudget) ?? 3000,
            full_rows_hard_row_cap: positiveIntOrNull(dbQaFullRowsHardRowCap) ?? 200,
            full_rows_hard_column_cap: positiveIntOrNull(dbQaFullRowsHardColumnCap) ?? 20,
            max_cell_chars_for_llm: positiveIntOrNull(dbQaMaxCellCharsForLlm) ?? 500,
            result_materialization_row_cap: positiveIntOrNull(dbQaResultMaterializationRowCap) ?? 99999,
            query_timeout_ms: (positiveIntOrNull(dbQaQueryTimeoutSeconds) ?? 30) * 1000,
            sql_generation_timeout_ms: (positiveIntOrNull(dbQaSqlGenerationTimeoutSeconds) ?? 210) * 1000,
            result_store_enabled: dbQaResultStoreEnabled,
            result_store_ttl_hours: positiveIntOrNull(dbQaResultStoreTtlHours) ?? 168,
            default_page_size: positiveIntOrNull(dbQaDefaultPageSize) ?? 100,
            max_page_size: positiveIntOrNull(dbQaMaxPageSize) ?? 500,
            export_enabled: dbQaExportEnabled,
            profile_enabled: dbQaProfileEnabled,
            database_agent_sql_fallback_enabled: dbQaAgentSqlFallbackEnabled,
          },
        }} : {}),
        ...(activeCategory === "database" ? { database: {
          provider: databaseMode === "sqlite" ? "sqlite" : "postgresql",
          source: databaseMode === "sqlite" ? "local_file" : "external",
          host: databaseHost || "127.0.0.1",
          port: positiveIntOrNull(databasePort) ?? 5432,
          database: databaseName || "puddingclaw",
          username: databaseUsername || "puddingclaw",
          password: databasePassword,
          // sqlite 模式下不发送 url 字段：空串会清掉 config.json 里用户手配的完整连接 URL。
          ...(databaseMode === "sqlite" ? {} : { url: "" }),
        }} : {}),
        ...(runtimeExtensions?.knowledge ? { knowledge: {
          root_dir: knowledgeRootDir,
          llm_wiki: {
            compiler_agent: {
              model_id: wikiCompilerModelId,
            },
            retrieval: {
              hybrid_enabled: wikiHybridEnabled,
            },
            gbrain: {
              embedding_model_id: wikiGbrainEmbeddingModelId,
              think_model_id: wikiGbrainThinkModelId,
            },
          },
          multimodal_index: {
            enabled: kbIndexEnabled,
            vector_store: kbVectorStore,
            milvus_uri: kbMilvusUri,
            text_collection: kbTextCollection,
            image_collection: kbImageCollection,
            embedding_batch_size: Number.parseInt(mmBatchSize, 10) || 10,
          },
        }} : {}),
        compression: {
          deepagents: {
            summarization: {
              model_id: contextSummaryModelId,
              trigger_tokens: summaryTrigger,
              keep_tokens: summaryKeep,
            },
            tool_context: {
              enabled: toolContextEnabled,
              immediate_compaction_enabled: immediateToolCompactionEnabled,
              single_tool_trigger_tokens: singleToolTrigger,
              background_min_result_tokens: backgroundMinimum,
              retain_tool_context_tokens: retainedToolTokens,
            },
          },
        },
        harness: {
          prompt_cache: {
            trace_part_diagnostics: tracePartDiagnostics,
            ordered_system_sections: orderedSystemSections,
            tail_routing_message: tailRoutingMessage,
            deterministic_session_projection: deterministicSessionProjection,
            stable_tool_schema: stableToolSchema,
          },
          model_call_limit: {
            enabled: modelCallLimitEnabled,
            run_limit: positiveIntOrNull(modelCallRunLimit) ?? 50,
            thread_limit: positiveIntOrNull(modelCallThreadLimit),
            exit_behavior: modelCallExitBehavior,
          },
          completion: {
            rubric: {
              enabled: rubricEnabled,
              max_iterations: positiveIntOrNull(rubricMaxIterations) ?? 3,
              max_stagnant_repairs: positiveIntOrNull(rubricMaxStagnantRepairs) ?? 2,
              custom_rules_enabled: customRubricRulesEnabled,
              custom_rules: customRubricRules.filter((rule) => rule.statement.trim()),
            },
          },
          goals: {
            enabled: goalsEnabled,
            activation: "explicit_user_only",
            default_enabled: false,
            auto_promote_from_run: false,
            max_rounds: positiveIntOrNull(goalMaxRounds) ?? 8,
          },
          terminal: {
            execution_mode: executionMode,
            default_timeout_seconds: 120,
          },
        },
        subagents: subagentItemsToConfig(subagentItems),
      };
      const saveResult = await updateSettings(updates);
      if (activeCategory === "database" && saveResult?.requires_migration) {
        // 后端在 PostgreSQL → SQLite 切换时返回迁移警告：新 Catalog 为空，
        // 必须明确告知用户，不能伪装成普通"保存成功"。
        showToast("error", saveResult.migration_warning || "数据库提供方已切换：新的 Catalog 为空，原有数据不会自动迁移。");
      } else {
        showToast("success", "设置已保存，将从下一次 Agent 运行生效");
      }
      const fresh = await getSettings();
      setDatabaseConfiguredBy(fresh.database?.configured_by || "default");
      setDatabaseSource(fresh.database?.source || "config");
      setDatabaseCatalogPath(fresh.database?.catalog_path || "$PUDDINGCLAW_HOME/db/catalog.sqlite3");
      setDatabaseEnvOverride(Boolean(fresh.database?.environment_override));
      const freshProvider = fresh.database?.provider;
      setDatabaseAppliedProvider(
        freshProvider === "postgresql" || freshProvider === "sqlite"
          ? freshProvider
          : fresh.database?.mode === "sqlite" ? "sqlite" : "postgresql",
      );
      setKnowledgeConfiguredBy(fresh.knowledge?.configured_by || "default");
      setKnowledgeEnvOverride(Boolean(fresh.knowledge?.environment_override));
      setWikiHybridEnabled(fresh.knowledge?.llm_wiki?.retrieval?.hybrid_enabled ?? true);
      if (runtimeExtensions?.knowledge) {
        getLlmWikiWorkspaceStatus().then(setGbrainWorkspace).catch(() => {});
      }
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }, [activeCategory, ragTopK, ragThreshold, ragTextVectorWeight, ragImageVectorWeight, ragBm25Weight, ragHybridCandidateTopK, ragRerankEnabled, ragRerankCandidateTopK, dbQaFullRowsTokenBudget, dbQaPreviewRowsTokenBudget, dbQaProfileTokenBudget, dbQaFullRowsHardRowCap, dbQaFullRowsHardColumnCap, dbQaMaxCellCharsForLlm, dbQaResultMaterializationRowCap, dbQaQueryTimeoutSeconds, dbQaSqlGenerationTimeoutSeconds, dbQaResultStoreEnabled, dbQaResultStoreTtlHours, dbQaDefaultPageSize, dbQaMaxPageSize, dbQaExportEnabled, dbQaProfileEnabled, dbQaAgentSqlFallbackEnabled, databaseMode, databaseHost, databasePort, databaseName, databaseUsername, databasePassword, mmBatchSize, knowledgeRootDir, wikiCompilerModelId, wikiHybridEnabled, wikiGbrainEmbeddingModelId, wikiGbrainThinkModelId, kbIndexEnabled, kbVectorStore, kbMilvusUri, kbTextCollection, kbImageCollection, contextSummaryTriggerTokens, contextSummaryKeepTokens, toolContextEnabled, immediateToolCompactionEnabled, singleToolTriggerTokens, backgroundMinResultTokens, retainToolContextTokens, modelCallLimitEnabled, modelCallRunLimit, modelCallThreadLimit, modelCallExitBehavior, rubricEnabled, rubricMaxIterations, rubricMaxStagnantRepairs, customRubricRulesEnabled, customRubricRules, goalsEnabled, goalMaxRounds, executionMode, subagentItems, showToast, runtimeExtensions]);

  const handleWikiHybridChange = useCallback(async (enabled: boolean) => {
    if (wikiHybridSaving) return;
    const previous = wikiHybridEnabled;
    setWikiHybridEnabled(enabled);
    setWikiHybridSaving(true);
    try {
      await updateSettings({
        knowledge: {
          llm_wiki: {
            retrieval: { hybrid_enabled: enabled },
          },
        },
      });
      const fresh = await getSettings();
      const persisted = fresh.knowledge?.llm_wiki?.retrieval?.hybrid_enabled ?? true;
      if (persisted !== enabled) {
        throw new Error("后端未返回刚保存的混合检索配置");
      }
      setWikiHybridEnabled(persisted);
      showToast("success", enabled ? "Wiki 混合检索已开启" : "Wiki 混合检索已关闭");
    } catch (err) {
      setWikiHybridEnabled(previous);
      showToast("error", err instanceof Error ? err.message : "混合检索配置保存失败");
    } finally {
      setWikiHybridSaving(false);
    }
  }, [showToast, wikiHybridEnabled, wikiHybridSaving]);

  const handleDatabaseModeChange = useCallback((mode: "sqlite" | "bundled" | "external") => {
    if (mode === "sqlite" && databaseAppliedProvider === "postgresql" && databaseMode !== "sqlite") {
      // 当前生效的是 PostgreSQL：切到 SQLite 会产生空 Catalog，先二次确认。
      const confirmed = window.confirm(
        "切换为 SQLite 将产生空 Catalog：PostgreSQL 中的原有数据不会自动迁移。确定继续切换吗？"
      );
      if (!confirmed) return;
    }
    setDatabaseMode(mode);
    setDatabaseHost("127.0.0.1");
    setDatabasePort("5432");
    if (mode === "sqlite") {
      // SQLite 不使用数据库名字段（输入框禁用），回填合理默认而非文件名。
      setDatabaseName("puddingclaw");
      setDatabaseUsername("");
      setDatabasePassword("");
    } else if (mode === "bundled") {
      setDatabaseName("puddingclaw");
      setDatabaseUsername("puddingclaw");
      setDatabasePassword("");
    } else if (mode === "external") {
      setDatabaseName("puddingclaw");
      setDatabaseUsername("");
      setDatabasePassword("");
    }
  }, [databaseAppliedProvider, databaseMode]);

  const databaseConnectionPayload = useCallback((createIfMissing = false) => ({
    mode: databaseMode,
    host: databaseHost || "127.0.0.1",
    port: positiveIntOrNull(databasePort) ?? 5432,
    database: databaseName || "puddingclaw",
    username: databaseUsername || "puddingclaw",
    password: databasePassword,
    create_if_missing: createIfMissing,
  }), [databaseHost, databaseMode, databaseName, databasePassword, databasePort, databaseUsername]);

  const handleTestDatabase = useCallback(async () => {
    if (databaseMode === "sqlite") {
      setDatabaseTesting(true);
      setDatabaseTestResult(null);
      try {
        const freshCapabilities = await getCapabilities();
        setCapabilities(freshCapabilities);
        const coreDatabase = freshCapabilities.core_database || freshCapabilities.database;
        if (!coreDatabase?.available) {
          const message = coreDatabase?.reason || "SQLite Catalog 当前不可用";
          setDatabaseTestResult({ ok: false, msg: message });
          showToast("error", message);
          return;
        }
        setDatabaseTestResult({ ok: true, msg: `SQLite Catalog 可用：${databaseCatalogPath}` });
        showToast("success", "SQLite Catalog 可用");
      } catch (err) {
        const message = err instanceof Error ? err.message : "SQLite Catalog 检查失败";
        setDatabaseTestResult({ ok: false, msg: message });
        showToast("error", message);
      } finally {
        setDatabaseTesting(false);
      }
      return;
    }
    setDatabaseTesting(true);
    setDatabaseTestResult(null);
    try {
      const result = await testDatabaseConnection(databaseConnectionPayload(false));
      if (result.success) {
        if (runtimeExtensions?.knowledge && result.pgvector && !result.pgvector.available) {
          const message = `PostgreSQL 已连接，但缺少必备 pgvector。请运行：${result.pgvector.install_command}`;
          setDatabaseTestResult({ ok: false, msg: message });
          showToast("error", "PostgreSQL 缺少 pgvector");
        } else {
          const pgvectorDetail = result.pgvector?.available
            ? " · pgvector 可用"
            : runtimeExtensions?.knowledge
              ? ""
              : " · pgvector 未安装（仅知识库需要）";
          setDatabaseTestResult({ ok: true, msg: result.created ? `数据库已创建并连接成功${pgvectorDetail}` : `连接成功${pgvectorDetail} · ${result.latency_ms}ms` });
          showToast("success", result.created ? "数据库已创建并连接成功" : "数据库连接成功");
        }
      } else if (result.database_missing && result.can_create) {
        setDatabaseTesting(false);
        const shouldCreate = window.confirm(
          `数据库 “${databaseName || "puddingclaw"}” 不存在。是否现在创建？`
        );
        if (!shouldCreate) {
          setDatabaseTestResult({ ok: false, msg: "数据库不存在，已取消创建" });
          return;
        }
        setDatabaseTesting(true);
        const created = await testDatabaseConnection(databaseConnectionPayload(true));
        if (runtimeExtensions?.knowledge && created.pgvector && !created.pgvector.available) {
          setDatabaseTestResult({ ok: false, msg: `数据库已创建，但缺少必备 pgvector。请运行：${created.pgvector.install_command}` });
          showToast("error", "数据库已创建，但 pgvector 未安装");
        } else {
          const pgvectorDetail = created.pgvector?.available
            ? " · pgvector 可用"
            : runtimeExtensions?.knowledge
              ? ""
              : " · pgvector 未安装（仅知识库需要）";
          setDatabaseTestResult({ ok: true, msg: `数据库已创建并连接成功${pgvectorDetail} · ${created.latency_ms}ms` });
          showToast("success", "数据库已创建并连接成功");
        }
      } else {
        setDatabaseTestResult({ ok: false, msg: result.message || "数据库连接失败" });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "数据库连接失败";
      setDatabaseTestResult({ ok: false, msg });
      showToast("error", msg);
    } finally {
      setDatabaseTesting(false);
    }
  }, [databaseCatalogPath, databaseConnectionPayload, databaseMode, databaseName, runtimeExtensions?.knowledge, showToast]);

  const gbrainDatabaseConnectionPayload = useCallback((createIfMissing = false) => ({
    mode: "external" as const,
    host: gbrainDatabaseHost || "127.0.0.1",
    port: positiveIntOrNull(gbrainDatabasePort) ?? 5432,
    database: gbrainDatabaseName || "llm_wiki",
    username: gbrainDatabaseUsername || "puddingclaw",
    password: gbrainDatabasePassword,
    create_if_missing: createIfMissing,
  }), [gbrainDatabaseHost, gbrainDatabaseName, gbrainDatabasePassword, gbrainDatabasePort, gbrainDatabaseUsername]);

  const ensureGbrainDatabase = useCallback(async () => {
    let result = await testDatabaseConnection(gbrainDatabaseConnectionPayload(false));
    if (result.database_missing && result.can_create) {
      const shouldCreate = window.confirm(
        `gbrain 独立数据库“${gbrainDatabaseName || "llm_wiki"}”不存在。是否现在创建？`
      );
      if (!shouldCreate) return result;
      result = await testDatabaseConnection(gbrainDatabaseConnectionPayload(true));
    }
    return result;
  }, [gbrainDatabaseConnectionPayload, gbrainDatabaseName]);

  const handleTestGbrainDatabase = useCallback(async () => {
    setGbrainDatabaseTesting(true);
    setGbrainDatabaseTestResult(null);
    try {
      const result = await ensureGbrainDatabase();
      if (!result.success) {
        setGbrainDatabaseTestResult({ ok: false, msg: result.message || "gbrain 数据库连接失败" });
        return;
      }
      if (result.pgvector && !result.pgvector.available) {
        const message = `PostgreSQL 已连接，但缺少必备 pgvector。请运行：${result.pgvector.install_command}`;
        setGbrainDatabaseTestResult({ ok: false, msg: message });
        showToast("error", "PostgreSQL 缺少 pgvector");
        return;
      }
      const message = result.created
        ? "gbrain 独立数据库已创建并连接成功，pgvector 可用"
        : `连接成功 · pgvector 可用 · ${result.latency_ms}ms`;
      setGbrainDatabaseTestResult({ ok: true, msg: message });
      showToast("success", "gbrain 数据库连接成功");
    } catch (err) {
      const message = err instanceof Error ? err.message : "gbrain 数据库连接失败";
      setGbrainDatabaseTestResult({ ok: false, msg: message });
      showToast("error", message);
    } finally {
      setGbrainDatabaseTesting(false);
    }
  }, [ensureGbrainDatabase, showToast]);

  const handleInitializeGbrain = useCallback(async () => {
    setGbrainInitializing(true);
    setGbrainDatabaseTestResult(null);
    try {
      const result = await ensureGbrainDatabase();
      if (!result.success) throw new Error(result.message || "gbrain 数据库连接失败");
      if (result.pgvector && !result.pgvector.available) {
        throw new Error(`PostgreSQL 缺少必备 pgvector。请运行：${result.pgvector.install_command}`);
      }
      const payload = gbrainDatabaseConnectionPayload(false);
      await initializeLlmWikiGbrain(postgresConnectionUrl({
        host: payload.host,
        port: payload.port,
        database: payload.database,
        username: payload.username,
        password: payload.password || "",
      }));
      const workspace = await getLlmWikiWorkspaceStatus();
      setGbrainWorkspace(workspace);
      setGbrainDatabaseTestResult({ ok: true, msg: "gbrain 数据库已连接，Schema Pack 已安装" });
      showToast("success", "gbrain 数据库配置已生效");
    } catch (err) {
      const message = err instanceof Error ? err.message : "gbrain 初始化失败";
      setGbrainDatabaseTestResult({ ok: false, msg: message });
      showToast("error", message);
    } finally {
      setGbrainInitializing(false);
    }
  }, [ensureGbrainDatabase, gbrainDatabaseConnectionPayload, showToast]);

  const handleResetVectorCollections = useCallback(async () => {
    const confirmed = window.confirm(
      "确认清空知识库索引吗？\n\n这只会删除已生成的检索索引，不会删除你上传的 PDF、Markdown 或图片文件。"
    );
    if (!confirmed) return;
    setSaving(true);
    try {
      const result = await resetKnowledgeVectorCollections();
      showToast("success", result.dropped.length > 0 ? "知识库索引已清空" : "没有找到需要清空的索引");
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "操作失败");
    } finally {
      setSaving(false);
    }
  }, [showToast]);

  const handleRefreshAgentModels = useCallback(async () => {
    setRefreshingModels(true);
    try {
      await refreshProviders();
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "刷新模型列表失败");
    } finally {
      setRefreshingModels(false);
    }
  }, [refreshProviders, showToast]);

  const handleChooseKnowledgeFolder = useCallback(async () => {
    if (!window.electron?.selectKnowledgeFolder) {
      showToast("error", "当前环境不支持系统文件夹选择，请手动粘贴本地目录路径。");
      return;
    }
    const selected = await window.electron.selectKnowledgeFolder();
    if (selected?.trim()) {
      setKnowledgeRootDir(selected.trim());
    }
  }, [showToast]);

  const handleAddSubAgent = useCallback(() => {
    setSubagentItems((prev) => {
      const newItem: SubAgentItem = {
        enabled: false,
        name: `subagent_${prev.length + 1}`,
        model: agentModels[0] || "",
        description: "",
        route_trigger: "",
        tools: { mode: "inherit" },
        skills: { mode: "inherit", paths: [] },
        system_prompt: "",
      };
      const next = [...prev, newItem];
      setSelectedSubagentIndex(next.length - 1);
      return next;
    });
  }, [agentModels]);

  const handleToggleSubAgent = useCallback((index: number, checked: boolean) => {
    if (!checked) {
      updateSubAgentItem(index, (it) => ({ ...it, enabled: false }));
      return;
    }
    const item = subagentItems[index];
    if (!item) return;
    if (!isSubAgentValid(item)) {
      setSelectedSubagentIndex(index);
      showToast("error", "请完善 SubAgent 必填项（名称、模型、描述、System Prompt）后再启用。");
      return;
    }
    updateSubAgentItem(index, (it) => ({ ...it, enabled: true }));
  }, [subagentItems, updateSubAgentItem, showToast]);

  const handleDeleteSubAgent = useCallback((index: number) => {
    setSubagentItems((prev) => prev.filter((_, i) => i !== index));
    setSelectedSubagentIndex((prev) => (prev === index ? null : prev === null || prev < index ? prev : prev - 1));
  }, []);

  const scrollToHarnessSection = useCallback((id: string) => {
    setActiveHarnessSection(id);
    const el = document.getElementById(`harness-section-${id}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, []);

  const filteredHarnessSections = HARNESS_SECTIONS.filter(
    (s) =>
      s.label.toLowerCase().includes(harnessFilter.toLowerCase()) ||
      s.description.toLowerCase().includes(harnessFilter.toLowerCase())
  );

  // Update active harness section on scroll
  useEffect(() => {
    if (category !== "harness") return;
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const id = entry.target.id.replace("harness-section-", "");
            setActiveHarnessSection(id);
          }
        });
      },
      { rootMargin: "-10% 0px -70% 0px", threshold: 0 }
    );
    HARNESS_SECTIONS.forEach((s) => {
      const el = document.getElementById(`harness-section-${s.id}`);
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, [category]);

  if (loading) {
    return (
      <div className="h-screen app-bg">
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
            style={{ width: !mounted || sidebarOpen ? 208 : 0 }}
          >
            <div className="h-full w-52 flex flex-col">
              <div className="h-11 shrink-0" />
              <div className="flex-1 min-h-0 overflow-y-auto p-3" />
            </div>
          </div>
          <div className="workspace-content-frame flex-1 flex items-center justify-center">
            <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
          </div>
        </div>
      </div>
    );
  }

  const activeProvider = providerRegistry?.providers.find((provider) => provider.id === selectedProviderId) || null;
  const activeEndpoint = activeProvider?.endpoints.find((endpoint) => endpoint.id === selectedEndpointId) || activeProvider?.endpoints[0] || null;
  const activeEndpointKey = activeProvider && activeEndpoint ? `${activeProvider.id}:${activeEndpoint.id}` : "";
  const isAddingProviderCredential = Boolean(activeProvider && providerAddingCredentialId === activeProvider.id);
  const activeProviderCredentialName = activeProvider
    ? isAddingProviderCredential
      ? providerCredentialNames[activeProvider.id] ?? ""
      : providerCredentialNames[activeProvider.id] || "default"
    : "default";
  const activeProviderCredentialKey = activeProvider
    ? isAddingProviderCredential
      ? `${activeProvider.id}:__new__`
      : `${activeProvider.id}:${activeProviderCredentialName}`
    : "";
  const activeProviderCredential = activeProvider?.api_keys.find((item) => item.name === activeProviderCredentialName);
  const filteredProviders = (providerRegistry?.providers || []).filter((provider) => {
    const query = providerSearch.trim().toLowerCase();
    const searchable = `${provider.name} ${provider.id}`.toLowerCase();
    const matchesSearch = !query || searchable.includes(query);
    const matchesStatus = !onlyConfiguredProviders || provider.api_keys.some((item) => item.credential_configured);
    return matchesSearch && matchesStatus;
  });
  const allProviderModels = providerRegistry?.providers.flatMap((provider) => provider.models.map((model) => ({ ...model, provider }))) || [];
  const imageAnalyzerBoundId = providerRegistry?.bindings.image_analyzer || "";
  const imageAnalyzerProviderModels = allProviderModels.filter((model) => (
    model.capability === "llm"
    && (modelCategories(model).includes("multimodal_llm") || model.id === imageAnalyzerBoundId)
  ));
  const providerHasDefaultModel = (provider: ProviderService) => Object.values(providerRegistry?.bindings || {}).some(
    (modelId) => provider.models.some((model) => model.id === modelId),
  );
  const modelPickerProvider = providerRegistry?.providers.find((provider) => provider.id === providerModelPicker?.providerId) || null;
  const modelPickerEndpoint = modelPickerProvider?.endpoints.find((endpoint) => endpoint.id === providerModelPicker?.endpointId) || null;
  const modelPickerKey = modelPickerProvider && modelPickerEndpoint ? `${modelPickerProvider.id}:${modelPickerEndpoint.id}` : "";
  const modelPickerModels = (discoveredProviderModels[modelPickerKey] || [])
    .map((name) => {
      const existing = modelPickerProvider?.models.find((model) => model.endpoint_id === modelPickerEndpoint?.id && model.name === name);
      const categories = existing ? modelCategories(existing) : suggestModelCategories(name, modelPickerEndpoint?.capabilities || []);
      return {
        name,
        categories,
        filters: inferDiscoveredFilters(name, categories),
        family: discoveredModelFamily(name, modelPickerProvider?.name || "其他"),
      };
    })
    .filter((model) => {
      const matchesSearch = !providerModelSearch.trim() || model.name.toLowerCase().includes(providerModelSearch.trim().toLowerCase());
      return matchesSearch && matchesDiscoveredFilter(model.filters, providerModelFilter);
    });
  const groupedModelPickerModels = Object.entries(
    modelPickerModels.reduce<Record<string, typeof modelPickerModels>>((groups, model) => {
      (groups[model.family] ||= []).push(model);
      return groups;
    }, {}),
  ).sort(([left], [right]) => left.localeCompare(right));
  const pendingModelProvider = providerRegistry?.providers.find((provider) => provider.id === pendingModelCategoryEdit?.providerId) || null;
  const pendingModelEndpoint = pendingModelProvider?.endpoints.find((endpoint) => endpoint.id === pendingModelCategoryEdit?.endpointId) || null;
  const pendingCategoryCapability = pendingModelCategoryEdit?.capability
    || MODEL_CATEGORY_OPTIONS.find((option) => selectedModelCategories.includes(option.id))?.capability;
  const editableModelCategories = MODEL_CATEGORY_OPTIONS.filter((option) => (
    pendingModelEndpoint?.capabilities.includes(option.capability)
    && (!pendingModelCategoryEdit?.capability || option.capability === pendingModelCategoryEdit.capability)
    && (runtimeExtensions?.knowledge || option.capability === "llm")
  ));
  const showPageSave = activeCategory === "databaseQa"
    || activeCategory === "rag"
    || activeCategory === "knowledge"
    || activeCategory === "harness"
    || (activeCategory === "database" && !databaseEnvOverride);

  return (
    <div className="h-screen app-bg">
      <div className="fixed left-3 top-3 z-[80]">
        <Navbar
          sidebarOpen={sidebarOpen}
          toggleSidebar={toggleSidebar}
          showPanelToggles
          compact
        />
      </div>

      <div className="flex h-full overflow-hidden">
        {/* Left: Category Navigation */}
        <div
          className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden"
          style={{ width: !mounted || sidebarOpen ? 208 : 0 }}
        >
          <div className="h-full w-52 flex flex-col">
            <div className="h-11 shrink-0" />
            <SettingsNavigation active={activeCategory} extensions={runtimeExtensions} onSelectCategory={setCategory} onReturnToApp={handleReturnToApp} />
          </div>
        </div>

        {/* Right: Settings Form */}
        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto px-5 pb-10 pt-6 sm:px-8">
            <div className="mx-auto w-full max-w-6xl space-y-6">
              <SettingsWorkspaceHeader
                category={activeCategory}
                description={SETTINGS_CATEGORY_DESCRIPTIONS[activeCategory]}
                onSave={handleSave}
                saving={saving}
                showSave={showPageSave}
              />
              {activeCategory === "ai" && (
              <section className="provider-light min-h-[calc(100vh-96px)] overflow-hidden rounded-2xl border border-slate-200 bg-white text-slate-900 shadow-xl shadow-slate-200/50" data-screen-label="模型服务">
                <style jsx>{`
                  .provider-light :global([class*="border-white"]) { border-color: rgb(226 232 240 / 0.9) !important; }
                  .provider-light :global([class*="text-white"]) { color: #1e293b !important; }
                  .provider-light :global([class*="text-white/25"]) { color: #64748b !important; }
                  .provider-light :global([class*="text-white/30"]), .provider-light :global([class*="text-white/35"]) { color: #64748b !important; }
                  .provider-light :global([class*="text-white/40"]), .provider-light :global([class*="text-white/45"]) { color: #475569 !important; }
                  .provider-light :global([class*="text-white/55"]), .provider-light :global([class*="text-white/65"]) { color: #334155 !important; }
                  .provider-light :global([class*="text-white/75"]), .provider-light :global([class*="text-white/90"]) { color: #1e293b !important; }
                  .provider-light :global([class*="text-[#8d9cff]"]), .provider-light :global([class*="text-[#b1b8ff]"]), .provider-light :global([class*="text-[#b8bfff]"]) { color: #002fa7 !important; }
                  .provider-light :global([class*="text-emerald-300"]) { color: #047857 !important; }
                  .provider-light :global([class*="bg-[#161616]"]) { background-color: #ffffff !important; }
                  .provider-light :global([class*="bg-[#151515]"]) { background-color: #ffffff !important; }
                  .provider-light :global([class*="bg-[#171717]"]) { background-color: #f8fafc !important; }
                  .provider-light :global([class*="bg-[#1a1a1a]"]), .provider-light :global([class*="bg-[#1d1d1d]"]) { background-color: #ffffff !important; }
                  .provider-light :global([class*="bg-black/"]) { background-color: #f8fafc !important; }
                  .provider-light :global([class*="bg-white/[0.025]"]) { background-color: #ffffff !important; }
                  .provider-light :global([class*="bg-white/[0.04]"]) { background-color: #f8fafc !important; }
                  .provider-light :global([class*="bg-white/[0.055]"]) { background-color: #f8fafc !important; }
                  .provider-light :global([class*="bg-white/[0.06]"]), .provider-light :global([class*="bg-white/[0.07]"]), .provider-light :global([class*="bg-white/[0.08]"]) { background-color: #f1f5f9 !important; }
                  .provider-light :global([class*="bg-white/[0.1]"]) { background-color: #e8edff !important; }
                  .provider-light :global([class*="bg-white/[0.15]"]) { background-color: #cbd5e1 !important; }
                  .provider-light :global([class*="hover:bg-white/"]:hover) { background-color: #eef2ff !important; }
                  .provider-light :global([class*="focus:border-[#8d9cff]"]:focus) { border-color: #002fa7 !important; }
                  .provider-light :global(input::placeholder) { color: #94a3b8 !important; }
                  .provider-light :global(button[class*="bg-[#727eff]"]), .provider-light :global(button[class*="bg-emerald-500"]) { background-color: #002fa7 !important; color: #ffffff !important; }
                  .provider-light :global(button[class*="bg-[#727eff]"] *), .provider-light :global(button[class*="bg-emerald-500"] *) { color: #ffffff !important; }
                  .provider-light :global(.provider-primary-action) { background-color: #002fa7 !important; border: 1px solid #001f7a !important; color: #ffffff !important; }
                  .provider-light :global(.provider-primary-action:hover) { background-color: #001f7a !important; }
                `}</style>
                <div className="grid min-h-[calc(100vh-96px)] grid-cols-1 lg:grid-cols-[300px_minmax(0,1fr)]">
                  <aside className="border-b border-white/[0.1] bg-[#171717] lg:border-b-0 lg:border-r">
                    <div className="border-b border-white/[0.1] px-5 py-5">
                      <div className="mb-4 flex items-center gap-2 text-[13px] font-semibold tracking-[0.02em] text-white">
                        <Network className="h-4 w-4 text-[#8d9cff]" /> 模型服务
                      </div>
                      <div className="flex items-center gap-2 rounded-xl border border-white/[0.12] bg-black/20 px-3 py-2.5 transition-colors focus-within:border-[#8d9cff]/70">
                        <Search className="h-4 w-4 shrink-0 text-white/35" />
                        <input
                          name="provider-platform-filter"
                          autoComplete="off"
                          data-1p-ignore="true"
                          data-lpignore="true"
                          readOnly={!providerSearchEnabled}
                          onPointerDown={() => setProviderSearchEnabled(true)}
                          onFocus={() => setProviderSearchEnabled(true)}
                          value={providerSearch}
                          onChange={(event) => setProviderSearch(event.target.value)}
                          placeholder="搜索模型平台…"
                          className="min-w-0 flex-1 bg-transparent text-[13px] text-white outline-none placeholder:text-white/30"
                        />
                        <button type="button" onClick={() => setOnlyConfiguredProviders((current) => !current)} className={`rounded-md p-1 transition-colors ${onlyConfiguredProviders ? "bg-[#6875ff]/20 text-[#9da7ff]" : "text-white/40 hover:bg-white/[0.08] hover:text-white"}`} title="仅显示已配置的平台"><Filter className="h-4 w-4" /></button>
                      </div>
                    </div>

                    <div className="p-3">
                      <button type="button" onClick={() => setSelectedProviderId("defaults")} className={`mb-2 flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left transition-all ${selectedProviderId === "defaults" ? "bg-white/[0.1] text-white shadow-sm ring-1 ring-white/[0.12]" : "text-white/65 hover:bg-white/[0.055] hover:text-white"}`}>
                        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#6875ff]/15 text-[#aeb6ff]"><Route className="h-4 w-4" /></span>
                        <span className="min-w-0 flex-1"><span className="block text-[13px] font-semibold">默认模型</span><span className="mt-0.5 block text-[10px] text-white/40">为工作负载分配模型</span></span>
                      </button>
                      <div className="mb-2 px-3 pt-2 text-[10px] font-medium uppercase tracking-[0.12em] text-white/30">Provider</div>
                      <div className="space-y-1">
                        {filteredProviders.map((provider) => {
                          const selected = selectedProviderId === provider.id;
                          const configured = provider.api_keys.some((item) => item.credential_configured);
                          const hasDefaultModel = providerHasDefaultModel(provider);
                          return <button type="button" key={provider.id} onClick={() => setSelectedProviderId(provider.id)} className={`group flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-all ${selected ? "bg-white/[0.1] text-white shadow-sm ring-1 ring-white/[0.12]" : "text-white/65 hover:bg-white/[0.055] hover:text-white"}`}>
                            <ProviderLogo provider={provider} />
                            <span className="min-w-0 flex-1"><span className="block truncate text-[13px] font-semibold">{provider.name}</span><span className="mt-0.5 block text-[10px] text-white/40">{configured ? "已配置" : "待配置"}</span></span>
                            {hasDefaultModel && <span className="h-2.5 w-2.5 rounded-full bg-emerald-400" title="包含默认模型" aria-label="包含默认模型" />}
                          </button>;
                        })}
                        {filteredProviders.length === 0 && <div className="px-3 py-8 text-center text-[12px] text-white/35">没有匹配的平台</div>}
                      </div>
                    </div>
                  </aside>

                  <div className="min-w-0 bg-[#151515]">
                    {selectedProviderId === "defaults" && (
                      <div className="mx-auto max-w-4xl px-6 py-7 sm:px-10">
                        <div className="border-b border-white/[0.1] pb-6">
                          <div className="flex items-start gap-4"><span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-[#6875ff]/15 text-[#abb3ff]"><Route className="h-5 w-5" /></span><div><h1 className="text-[22px] font-semibold tracking-tight text-white">默认模型</h1><p className="mt-1 text-[12px] leading-5 text-white/45">为每类工作负载选择一个已登记模型。Provider 的地址与凭证不会在运行中切换。</p></div></div>
                        </div>
                        <div className="mt-7 grid gap-4 sm:grid-cols-2">
                          {([
                            ["agent", "默认助手模型", "对话、规划与工具调用"],
                            ["image_analyzer", "图片理解模型", "图片分析 SubAgent 使用，需选择支持视觉输入的对话模型"],
                            ["text_embedding", "文本 Embedding", "文档、表格与知识库文本"],
                            ["multimodal_embedding", "多模态 Embedding", "图片与图文混合内容"],
                            ["rerank", "Rerank", "召回结果的相关性重排"],
                          ] as const).filter(([binding]) => (
                            runtimeExtensions?.knowledge || binding === "agent" || binding === "image_analyzer"
                          )).map(([binding, label, description]) => {
                            const capability = binding === "agent" || binding === "image_analyzer" ? "llm" : binding;
                            const boundId = providerRegistry?.bindings[binding] || "";
                            const requiredCategory: ProviderModelCategory = binding === "agent"
                              ? "llm"
                              : binding === "image_analyzer"
                                ? "multimodal_llm"
                                : binding;
                            const models = allProviderModels.filter((model) => (
                              model.capability === capability
                              && (modelCategories(model).includes(requiredCategory) || model.id === boundId)
                            ));
                            const activeModel = models.find((model) => model.id === boundId);
                            return <div key={binding} className="rounded-2xl border border-white/[0.1] bg-white/[0.025] p-4 transition-colors hover:border-white/[0.16]">
                              <div className="flex items-start justify-between gap-3">
                                <p className="text-[13px] font-semibold text-white">{label}</p>
                                {activeModel && <button type="button" onClick={() => openModelCategoryEditor(activeModel.provider, activeModel.endpoint_id, activeModel.name, activeModel)} className="shrink-0 rounded-md px-2 py-1 text-[10px] font-medium text-white/55 transition hover:bg-white/[0.07] hover:text-white">编辑分类</button>}
                              </div>
                              <p className="mt-1 text-[11px] text-white/40">{description}</p>
                              <div className="mt-4"><ModelBindingSelect value={boundId} onChange={(modelId) => handleBindProvider(binding, modelId)} options={models.map((model) => ({ id: model.id, label: `${model.provider.name} · ${model.name}` }))} /></div>
                            </div>;
                          })}
                        </div>
                      </div>
                    )}

                    {activeProvider && activeEndpoint && selectedProviderId !== "defaults" && (
                      <div className="min-h-full">
                        <header className="flex min-h-[78px] items-center justify-between gap-4 border-b border-white/[0.1] px-6 sm:px-10">
                          <div className="flex min-w-0 items-center gap-3"><ProviderLogo provider={activeProvider} size="md" /><h1 className="truncate text-[20px] font-semibold tracking-tight text-white">{activeProvider.name}</h1>{activeProvider.website && <a href={activeProvider.website} target="_blank" rel="noreferrer" className="rounded-md p-1.5 text-white/55 transition hover:bg-white/[0.08] hover:text-white" title="打开 Provider 控制台"><ExternalLink className="h-4 w-4" /></a>}</div>
                        </header>

                        <div className="mx-auto max-w-5xl px-6 py-7 sm:px-10">
                          <section className="border-b border-white/[0.1] pb-7">
                            <div className="mb-3 flex items-center justify-between gap-4"><label className="text-[15px] font-semibold text-white">API 密钥</label><span className="text-[10px] text-white/35">按名称保存多个 Key；未选择时使用 default</span></div>
                            <div className="mb-3 flex flex-wrap gap-2">
                              {activeProvider.api_keys.map((credential) => (
                                <button key={credential.name} type="button" onClick={() => { setProviderAddingCredentialId(null); setProviderCredentialNames((current) => ({ ...current, [activeProvider.id]: credential.name })); setShowProviderKey(false); }} className={`rounded-lg px-3 py-1.5 text-[11px] font-medium transition ${!isAddingProviderCredential && activeProviderCredentialName === credential.name ? "bg-[#727eff] text-white" : "bg-white/[0.07] text-white/55 hover:bg-white/[0.11] hover:text-white"}`}>
                                  {credential.name}{credential.is_default ? " · 默认" : ""}{credential.credential_configured ? " ✓" : ""}
                                </button>
                              ))}
                              {isAddingProviderCredential ? (
                                <button type="button" onClick={() => { setProviderAddingCredentialId(null); setProviderCredentialNames((current) => ({ ...current, [activeProvider.id]: "default" })); setProviderKeys((current) => { const next = { ...current }; delete next[`${activeProvider.id}:__new__`]; return next; }); setShowProviderKey(false); }} className="inline-flex items-center gap-1.5 rounded-lg bg-rose-400/10 px-3 py-1.5 text-[11px] font-medium text-rose-300 transition hover:bg-rose-400/15"><X className="h-3.5 w-3.5" />取消新增</button>
                              ) : (
                                <button type="button" onClick={() => { setProviderAddingCredentialId(activeProvider.id); setProviderCredentialNames((current) => ({ ...current, [activeProvider.id]: "" })); setProviderKeys((current) => { const next = { ...current }; delete next[`${activeProvider.id}:__new__`]; return next; }); setShowProviderKey(false); }} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-100 px-3 py-1.5 text-[11px] font-medium text-slate-600 outline-none transition hover:border-slate-300 hover:bg-slate-200 hover:text-slate-800"><Plus className="h-3.5 w-3.5" />添加 Key</button>
                              )}
                            </div>
                            <div className="flex overflow-hidden rounded-xl border border-white/[0.13] bg-[#1a1a1a] transition-colors focus-within:border-[#8d9cff]">
                              <input value={activeProviderCredentialName} readOnly={!isAddingProviderCredential} onChange={(event) => setProviderCredentialNames((current) => ({ ...current, [activeProvider.id]: event.target.value }))} className={`w-36 border-r border-white/[0.13] px-4 py-3 font-mono text-[13px] text-white outline-none placeholder:text-white/25 ${isAddingProviderCredential ? "bg-transparent" : "cursor-default bg-white/[0.025] text-white/55"}`} placeholder="Key 名称" aria-label={`${activeProvider.name} Key 名称`} />
                              <input ref={providerKeyInputRef} type={showProviderKey ? "text" : "password"} name={`provider-api-key-${activeProvider.id}`} autoComplete="new-password" data-1p-ignore="true" data-lpignore="true" spellCheck={false} value={providerKeys[activeProviderCredentialKey] || ""} onChange={(event) => { setProviderKeys((current) => ({ ...current, [activeProviderCredentialKey]: event.target.value })); setProviderRevealedKeys((current) => { const next = { ...current }; delete next[activeProviderCredentialKey]; return next; }); }} placeholder={activeProviderCredential?.api_key_masked || "输入 API 密钥"} className="min-w-0 flex-1 bg-transparent px-4 py-3 text-[14px] text-white outline-none placeholder:text-white/25" aria-label={`${activeProvider.name} API Key`} />
                              <button type="button" disabled={providerBusy === `${activeProviderCredentialKey}:reveal`} onClick={() => handleProviderKeyVisibility(activeProvider, activeProviderCredentialName, Boolean(activeProviderCredential?.credential_configured))} className="px-3 text-white/45 transition hover:bg-white/[0.07] hover:text-white disabled:opacity-45" title={showProviderKey ? "隐藏密钥" : "显示密钥"}>{providerBusy === `${activeProviderCredentialKey}:reveal` ? <Loader2 className="h-4 w-4 animate-spin" /> : showProviderKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}</button>
                              {activeEndpoint.protocol !== "dashscope_multimodal_embedding" && <button type="button" disabled={providerBusy === `${activeEndpointKey}:test`} onClick={() => handleTestProviderConnection(activeProvider, activeEndpoint.id)} className="border-l border-white/[0.13] px-4 text-[12px] font-semibold text-white transition hover:bg-white/[0.07] disabled:opacity-45">{providerBusy === `${activeEndpointKey}:test` ? "检测中…" : "检测"}</button>}
                              <button type="button" disabled={providerBusy === activeEndpointKey} onClick={() => handleProviderSave(activeProvider, activeEndpoint.id, undefined, providerKeyInputRef.current?.value || providerKeys[activeProviderCredentialKey] || "", activeProviderCredentialName)} className="provider-primary-action border-l border-white/[0.13] px-4 text-[12px] font-semibold transition disabled:opacity-45">{providerBusy === activeEndpointKey ? "保存中…" : "保存"}</button>
                            </div>
                            {providerConnectionResults[activeEndpointKey] && <p className={`mt-2 text-[11px] ${providerConnectionResults[activeEndpointKey].ok ? "text-emerald-600" : "text-rose-600"}`}>{providerConnectionResults[activeEndpointKey].message}</p>}
                            {activeProvider.website && <a href={activeProvider.website} target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-1.5 text-[11px] text-[#8d9cff] transition hover:text-[#b1b8ff]"><KeyRound className="h-3.5 w-3.5" /> 前往 {activeProvider.name} 获取密钥</a>}
                          </section>

                          <section className="border-b border-white/[0.1] py-7">
                            <div className="mb-3 flex flex-wrap items-center justify-between gap-4"><div className="flex flex-wrap items-center gap-2"><label className="mr-1 text-[15px] font-semibold text-white">API 地址</label>{activeProvider.endpoints.length > 1 ? activeProvider.endpoints.map((endpoint) => <button type="button" key={endpoint.id} onClick={() => { setSelectedEndpointId(endpoint.id); setShowProviderKey(false); }} className={`rounded-md px-2.5 py-1 text-[10px] font-medium transition-colors ${activeEndpoint.id === endpoint.id ? "bg-[#727eff] text-white" : "bg-white/[0.07] text-white/45 hover:bg-white/[0.11] hover:text-white"}`}>{protocolLabel(endpoint.protocol)}</button>) : <span className="rounded-md bg-white/[0.07] px-2.5 py-1 text-[10px] text-white/40">{protocolLabel(activeEndpoint.protocol)}</span>}</div><button type="button" onClick={() => setProviderUrls((current) => ({ ...current, [activeEndpointKey]: activeEndpoint.base_url }))} className="inline-flex items-center gap-1.5 text-[11px] text-white/45 transition hover:text-white"><RotateCcw className="h-3.5 w-3.5" />撤销本次编辑</button></div>
                            <input value={providerUrls[activeEndpointKey] ?? activeEndpoint.base_url} onChange={(event) => setProviderUrls((current) => ({ ...current, [activeEndpointKey]: event.target.value }))} className="h-12 w-full rounded-xl border border-white/[0.13] bg-[#1a1a1a] px-4 font-mono text-[13px] text-white outline-none transition-colors focus:border-[#8d9cff]" aria-label={`${activeProvider.name} endpoint`} />
                            <p className="mt-3 flex items-center gap-1.5 text-[11px] text-white/35"><Globe2 className="h-3.5 w-3.5" />{activeEndpoint.protocol === "dashscope_multimodal_embedding" ? `${(providerUrls[activeEndpointKey] ?? activeEndpoint.base_url).replace(/\/$/, "")}${activeEndpoint.route_path || ""}` : `${(providerUrls[activeEndpointKey] ?? activeEndpoint.base_url).replace(/\/$/, "")}/models`}</p>
                            <div className="mt-5 flex justify-end"><button type="button" disabled={providerBusy === activeEndpointKey} onClick={() => handleProviderSave(activeProvider, activeEndpoint.id, providerUrls[activeEndpointKey] ?? activeEndpoint.base_url, "")} className="rounded-lg bg-[#727eff] px-4 py-2 text-[12px] font-semibold text-white transition hover:bg-[#8690ff] disabled:cursor-not-allowed disabled:opacity-50">{providerBusy === activeEndpointKey ? "保存中…" : "保存地址"}</button></div>
                          </section>

                          <section className="pt-7">
                            <div className="mb-5 flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-3"><h2 className="text-[16px] font-semibold text-white">全部模型</h2><span className="rounded-full bg-white/[0.08] px-2.5 py-1 text-[10px] text-white/45">{activeProvider.models.length}</span></div><button type="button" onClick={() => handleOpenProviderModelPicker(activeProvider, activeEndpoint.id)} className="inline-flex items-center gap-1.5 rounded-lg border border-white/[0.14] px-3 py-2 text-[11px] font-semibold text-white/75 transition hover:bg-white/[0.07] hover:text-white"><RefreshCw className="h-3.5 w-3.5" />获取当前接口模型</button></div>

                            <div className="space-y-3">
                              {MODEL_CATEGORY_OPTIONS.filter((categoryOption) => (
                                runtimeExtensions?.knowledge || categoryOption.capability === "llm"
                              )).map((categoryOption) => {
                                const models = activeProvider.models.filter((model) => modelCategories(model).includes(categoryOption.id));
                                if (!models.length) return null;
                                const groupKey = `${activeProvider.id}:${categoryOption.id}`;
                                const expanded = expandedModelGroups[groupKey] !== false;
                                return <div key={categoryOption.id} className="overflow-hidden rounded-xl border border-white/[0.1] bg-white/[0.025]">
                                  <button type="button" onClick={() => setExpandedModelGroups((current) => ({ ...current, [groupKey]: !expanded }))} className="flex w-full items-center gap-3 bg-white/[0.04] px-4 py-3 text-left transition hover:bg-white/[0.07]"><span className="text-white/45">{expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}</span><span className="flex-1 text-[13px] font-semibold text-white">{categoryOption.label}</span><span className="rounded-full bg-white/[0.07] px-2 py-0.5 text-[10px] text-white/45">{models.length}</span></button>
                                  {expanded && <div>{models.map((model) => { const binding = MODEL_CATEGORY_BINDINGS[categoryOption.id]; const isDefault = binding && providerRegistry?.bindings[binding] === model.id; const modelEndpoint = activeProvider.endpoints.find((endpoint) => endpoint.id === model.endpoint_id); return <div key={`${categoryOption.id}:${model.id}`} className="flex items-center gap-3 border-t border-white/[0.08] px-4 py-4"><ProviderLogo provider={activeProvider} size="sm" /><span className="min-w-0 flex-1 truncate font-mono text-[13px] font-medium text-white/90">{model.name}</span>{modelEndpoint && <span className="hidden rounded-md bg-white/[0.06] px-2 py-1 text-[10px] text-white/45 md:inline">{protocolLabel(modelEndpoint.protocol)}</span>}{model.dimension && <span className="hidden rounded-md bg-white/[0.06] px-2 py-1 text-[10px] text-white/45 sm:inline">{model.dimension} dim</span>}<button type="button" onClick={() => openModelCategoryEditor(activeProvider, model.endpoint_id, model.name, model)} className="rounded-full bg-white/[0.07] px-2.5 py-1 text-[10px] font-semibold text-white/65 transition hover:bg-white/[0.12] hover:text-white">编辑分类</button>{isDefault ? <span className="rounded-full bg-emerald-400/10 px-2.5 py-1 text-[10px] font-semibold text-emerald-300">默认</span> : binding && <button type="button" onClick={() => handleBindProvider(binding, model.id)} className="rounded-full bg-white/[0.07] px-2.5 py-1 text-[10px] font-semibold text-white/65 transition hover:bg-[#727eff]/25 hover:text-[#bec4ff]">设为默认</button>}</div>; })}</div>}
                                </div>;
                              })}
                              {activeProvider.models.length === 0 && <div className="rounded-xl border border-dashed border-white/[0.15] px-5 py-10 text-center text-[12px] text-white/40">尚未登记模型。可以从当前接口获取列表，或手动添加模型 ID。</div>}
                            </div>
                          </section>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </section>
            )}
            {activeCategory === "databaseQa" && (
              <SettingsAnchorLayout prefix="database-qa" sections={DATABASE_QA_SECTIONS}>
                <section id="database-qa-section-preview" className="scroll-mt-6 space-y-5">
                <div className="overflow-hidden rounded-2xl border border-[#002fa7]/10 bg-[#002fa7]/[0.025]">
                  <div className="border-b border-[#002fa7]/10 px-5 py-3.5">
                    <p className="text-[12px] font-semibold text-gray-800">一条查询结果的处理顺序</p>
                    <p className="mt-1 text-[10px] leading-4 text-gray-500">
                      这些设置不会改变 SQL 的业务口径，只决定结果以什么体量进入模型和是否保留完整副本。
                    </p>
                  </div>
                  <div className="grid gap-px bg-[#002fa7]/10 md:grid-cols-4">
                    {[
                      ["1", "执行 SQL", `最长 ${dbQaQueryTimeoutSeconds || "30"} 秒`],
                      ["2", "尝试完整直传", `行 ≤ ${dbQaFullRowsHardRowCap || "200"} · 列 ≤ ${dbQaFullRowsHardColumnCap || "20"}`],
                      ["3", "超限则发送预览", `预览 ≤ ${dbQaPreviewRowsTokenBudget || "3000"} Token`],
                      ["4", "保留完整结果", `物化 ≤ ${dbQaResultMaterializationRowCap || "5000"} 行`],
                    ].map(([step, label, detail]) => (
                      <div key={step} className="bg-white/90 px-4 py-3.5">
                        <div className="flex items-center gap-2">
                          <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[#002fa7] text-[9px] font-semibold text-white">
                            {step}
                          </span>
                          <span className="text-[11px] font-semibold text-gray-700">{label}</span>
                        </div>
                        <p className="mt-1.5 pl-7 text-[10px] leading-4 text-gray-400">{detail}</p>
                      </div>
                    ))}
                  </div>
                </div>

                <SettingsCard title="完整结果直传条件" icon={ShieldCheck} color="#0f172a">
                  <div className="rounded-xl bg-blue-50/60 px-3.5 py-3 text-[10px] leading-4 text-blue-700">
                    完整结果只有在<strong>行数、列数和估算 Token 三项同时达标</strong>时才会直接发送给模型。
                    这些条件不限制 SQL 实际返回多少行，也不决定完整结果能否落盘。
                  </div>
                  <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-3 text-[10px] text-slate-600">
                    <span className="font-semibold text-slate-800">完整直传 =</span>
                    <span className="rounded-full bg-white px-2 py-1 shadow-sm">行数 ≤ {dbQaFullRowsHardRowCap || "200"}</span>
                    <span className="font-semibold text-slate-400">AND</span>
                    <span className="rounded-full bg-white px-2 py-1 shadow-sm">列数 ≤ {dbQaFullRowsHardColumnCap || "20"}</span>
                    <span className="font-semibold text-slate-400">AND</span>
                    <span className="rounded-full bg-white px-2 py-1 shadow-sm">结果 ≤ {dbQaFullRowsTokenBudget || "10000"} Token</span>
                  </div>
                  <div className="grid gap-4 md:grid-cols-3">
                    <DatabaseQaParameterField
                      label="最大行数"
                      description="完整结果超过该行数时改发预览；不是 SQL 查询或落盘上限。"
                      unit="行"
                      value={dbQaFullRowsHardRowCap}
                      onChange={setDbQaFullRowsHardRowCap}
                    />
                    <DatabaseQaParameterField
                      label="最大列数"
                      description="宽表超过该列数时改发预览，避免一次占满模型上下文。"
                      unit="列"
                      value={dbQaFullRowsHardColumnCap}
                      onChange={setDbQaFullRowsHardColumnCap}
                    />
                    <DatabaseQaParameterField
                      label="最大内容体量"
                      description="完整结果经过单元格截短后的近似 Token 上限。"
                      unit="Token"
                      value={dbQaFullRowsTokenBudget}
                      onChange={setDbQaFullRowsTokenBudget}
                    />
                  </div>
                  <div className="border-t border-slate-100 pt-4">
                    <div className="grid gap-4 md:grid-cols-2">
                      <DatabaseQaParameterField
                        label="SQL 执行超时"
                        description="只计算数据库执行时间，不包含 SQL 生成、模型思考和后续文件写入。"
                        unit="秒"
                        value={dbQaQueryTimeoutSeconds}
                        onChange={setDbQaQueryTimeoutSeconds}
                      />
                      <DatabaseQaParameterField
                        label="SQL 生成总超时"
                        description="覆盖召回、候选生成、实体画像、语义修正和确定性预检；最后 30 秒仅用于收尾。"
                        unit="秒"
                        value={dbQaSqlGenerationTimeoutSeconds}
                        onChange={setDbQaSqlGenerationTimeoutSeconds}
                      />
                    </div>
                  </div>
                </SettingsCard>

                <SettingsCard title="预览与摘要内容" icon={Database} color="#002fa7">
                  <div className="rounded-xl bg-blue-50/60 px-3.5 py-3 text-[10px] leading-4 text-blue-700">
                    完整结果无法直传时，模型收到的是<strong>预览行 + Profile 摘要</strong>。
                    以下 Token 是本次数据库 Tool Result 的近似预算，不是模型的 272k 总上下文窗口。
                  </div>
                  <div className="grid gap-4 md:grid-cols-2">
                    <DatabaseQaParameterField
                      label="预览内容上限"
                      description="完整结果无法直传时，逐步减少预览行，直到预览内容落入该预算。"
                      unit="Token"
                      value={dbQaPreviewRowsTokenBudget}
                      onChange={setDbQaPreviewRowsTokenBudget}
                    />
                    <div className={`rounded-xl border px-3.5 py-3 ${dbQaProfileEnabled ? "border-blue-100 bg-blue-50/35" : "border-amber-100 bg-amber-50/40"}`}>
                      <div className="mb-3 flex items-start justify-between gap-3">
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="text-[11px] font-semibold text-gray-700">生成 Profile 摘要</p>
                            <span className="rounded-full bg-white px-2 py-0.5 text-[9px] font-medium text-[#002fa7] shadow-sm">
                              默认开启
                            </span>
                          </div>
                          <p className="mt-1 text-[10px] leading-4 text-gray-400">
                            进入预览模式时，补充分布、日期范围和数值范围，避免模型只依据预览行判断。
                          </p>
                        </div>
                        <SwitchButton
                          checked={dbQaProfileEnabled}
                          onChange={setDbQaProfileEnabled}
                          ariaLabel="生成 Profile 摘要"
                        />
                      </div>
                      <DatabaseQaParameterField
                        label="摘要内容上限"
                        description={dbQaProfileEnabled
                          ? "限制 Profile 进入模型的近似体量；不影响完整结果文件。"
                          : "已关闭：模型在结果超限时只会收到预览行。"}
                        unit="Token"
                        value={dbQaProfileTokenBudget}
                        onChange={setDbQaProfileTokenBudget}
                        disabled={!dbQaProfileEnabled}
                      />
                    </div>
                    <DatabaseQaParameterField
                      label="单个文本值最大长度"
                      description="完整直传和预览都会应用：单个文本超过该长度时，模型只看到前 N 个字符和省略号；数字不截断，落盘仍保存原值。"
                      unit="字符"
                      value={dbQaMaxCellCharsForLlm}
                      onChange={setDbQaMaxCellCharsForLlm}
                    />
                  </div>
                </SettingsCard>
                <SettingsCard title="SQL 可靠性" icon={Route} color="#7c3aed">
                  <ToggleRow
                    label="允许基础设施故障回退"
                    description="仅当 evidence search 超时、数据库不可用或 Agent 协议不可用时，允许临时调用兼容生成器；业务歧义、越权和证据不足不会回退。"
                    checked={dbQaAgentSqlFallbackEnabled}
                    onChange={setDbQaAgentSqlFallbackEnabled}
                  />
                </SettingsCard>
                </section>

                <section id="database-qa-section-storage" className="scroll-mt-6">
                  <SettingsCard title="持久化存储" icon={FileText} color="#10b981">
                  <ToggleRow
                    label="持久化结果集"
                    description="为超出直传条件的完整结果生成 result_id 和 JSONL；关闭后不落盘，也不提供后续分页与导出。"
                    checked={dbQaResultStoreEnabled}
                    onChange={setDbQaResultStoreEnabled}
                  />
                  <div className={`grid gap-4 rounded-xl border p-3.5 transition-opacity md:grid-cols-2 ${
                    dbQaResultStoreEnabled
                      ? "border-emerald-100 bg-emerald-50/20"
                      : "border-slate-100 bg-slate-50/60 opacity-50"
                  }`}>
                    <DatabaseQaParameterField
                      label="单个结果集最大行数"
                      description="开启持久化后，只有完整结果不超过该行数才会生成 result_id 并落盘。"
                      unit="行"
                      value={dbQaResultMaterializationRowCap}
                      onChange={setDbQaResultMaterializationRowCap}
                      disabled={!dbQaResultStoreEnabled}
                    />
                    <DatabaseQaParameterField
                      label="结果保留时间"
                      description="仅适用于已持久化的结果；到期后 JSONL、分页与导出入口会被清理。"
                      unit="小时"
                      value={dbQaResultStoreTtlHours}
                      onChange={setDbQaResultStoreTtlHours}
                      disabled={!dbQaResultStoreEnabled}
                    />
                    <div className="md:col-span-2">
                      <ToggleRow
                        label="允许导出"
                        description="控制已持久化结果页的 CSV 导出按钮和后端导出 API。"
                        checked={dbQaExportEnabled}
                        onChange={setDbQaExportEnabled}
                        disabled={!dbQaResultStoreEnabled}
                      />
                    </div>
                  </div>
                  <div className={`rounded-xl px-3.5 py-3 text-[10px] leading-4 ${
                    dbQaResultStoreEnabled
                      ? "bg-emerald-50/70 text-emerald-700"
                      : "bg-slate-50 text-slate-500"
                  }`}>
                    {dbQaResultStoreEnabled ? (
                      <>
                        落盘条件：结果未完整直传，且完整结果行数不超过
                        <strong> {dbQaResultMaterializationRowCap || "5000"} 行</strong>。成功后 Trace
                        会显示以 <code>qr_</code> 开头的 result_id、文件路径和过期时间。
                      </>
                    ) : (
                      "持久化已关闭：查询仍会返回模型预览和 Profile，但不会生成 result_id 或结果文件。"
                    )}
                  </div>
                  <div className={`border-t pt-4 ${dbQaResultStoreEnabled ? "border-emerald-100" : "border-slate-100 opacity-50"}`}>
                    <div className="mb-3">
                      <p className="text-[12px] font-semibold text-gray-800">分页读取</p>
                      <p className="mt-1 text-[10px] leading-4 text-gray-500">
                        控制模型或结果页每次从已持久化文件中读取多少行。
                      </p>
                    </div>
                    <div className="grid gap-4 md:grid-cols-2">
                      <DatabaseQaParameterField
                        label="默认每页行数"
                        description="未指定 page_size 时使用。"
                        unit="行"
                        value={dbQaDefaultPageSize}
                        onChange={setDbQaDefaultPageSize}
                        disabled={!dbQaResultStoreEnabled}
                      />
                      <DatabaseQaParameterField
                        label="单页最大行数"
                        description="限制单次分页请求体量，不会提高持久化行数上限。"
                        unit="行"
                        value={dbQaMaxPageSize}
                        onChange={setDbQaMaxPageSize}
                        disabled={!dbQaResultStoreEnabled}
                      />
                    </div>
                  </div>
                  </SettingsCard>
                </section>
              </SettingsAnchorLayout>
            )}

            {/* RAG Settings */}
            {activeCategory === "rag" && (
              <SettingsAnchorLayout prefix="rag" sections={RAG_SECTIONS}>
                <section id="rag-section-recall" className="scroll-mt-6">
                <SettingsCard title="基础召回" icon={Search} color="#002fa7">
                <div className="rounded-lg border border-black/[0.06] bg-white/50 px-3.5 py-3">
                  <p className="text-[12px] font-medium text-gray-700">知识库检索默认开启</p>
                  <p className="mt-0.5 text-[11px] leading-5 text-gray-500">
                    这里配置 LlamaIndex 工具的召回策略。Top-K 是最终交给 Agent/LLM 的结果数量；候选数量是中间召回池。
                  </p>
                </div>
                <FormField label={`最终结果数 Top-K: ${ragTopK}`}>
                  <input
                    type="range"
                    min="1"
                    max="10"
                    step="1"
                    value={ragTopK}
                    onChange={(e) => setRagTopK(parseInt(e.target.value))}
                    className="w-full accent-[#002fa7]"
                  />
                  <div className="flex justify-between text-[10px] text-gray-400 mt-0.5">
                    <span>精确 (1)</span>
                    <span>广泛 (10)</span>
                  </div>
                </FormField>
                <FormField label={`相似度阈值: ${ragThreshold}`}>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={ragThreshold}
                    onChange={(e) => setRagThreshold(parseFloat(e.target.value))}
                    className="w-full accent-[#002fa7]"
                  />
                  <div className="flex justify-between text-[10px] text-gray-400 mt-0.5">
                    <span>宽松 (0)</span>
                    <span>严格 (1)</span>
                  </div>
                </FormField>
				</SettingsCard>
				</section>
				<section id="rag-section-hybrid" className="scroll-mt-6">
				<SettingsCard title="混合检索" icon={Network} color="#002fa7">
				<div className="rounded-xl border border-[#002fa7]/10 bg-[#002fa7]/[0.04] p-3.5 space-y-3">
	                  <div className="min-w-0">
	                    <p className="text-[12px] font-semibold text-gray-800">混合检索权重</p>
	                    <p className="mt-0.5 text-[11px] text-gray-500">
	                      先做文本混合检索，再和图片结果一起排序。
	                    </p>
	                  </div>
	                  <FormField label={`召回池大小: ${ragHybridCandidateTopK}`}>
	                    <input
	                      type="range"
	                      min="3"
                      max="50"
                      step="1"
                      value={ragHybridCandidateTopK}
                      onChange={(e) => setRagHybridCandidateTopK(parseInt(e.target.value))}
	                      className="w-full accent-[#002fa7]"
	                    />
	                  </FormField>
	                  <div className="rounded-2xl border border-black/[0.04] bg-white/60 p-3">
	                    <div className="mb-3 flex items-center justify-between gap-3">
	                      <p className="text-[11px] font-semibold text-gray-700">文本混合检索</p>
	                      <p className="text-[11px] text-gray-400">合计 100%</p>
	                    </div>
	                    <div className="flex items-center justify-between text-[12px] font-medium text-gray-600">
	                      <span>关键词匹配 {Math.round(ragBm25Weight * 100)}%</span>
	                      <span>语义理解 {Math.round(ragTextVectorWeight * 100)}%</span>
	                    </div>
	                    <input
	                      type="range"
	                      min="0"
	                      max="1"
	                      step="0.05"
	                      value={ragTextVectorWeight}
	                      onChange={(e) => setRagTextVectorWeight(parseFloat(e.target.value))}
	                      className="mt-3 w-full accent-[#002fa7]"
	                    />
	                    <div className="mt-1 flex justify-between text-[10px] text-gray-400">
	                      <span>更偏关键词</span>
	                      <span>更偏语义</span>
	                    </div>
	                  </div>
	                  <div className="rounded-2xl border border-black/[0.04] bg-white/60 p-3">
	                    <div className="mb-3 flex items-center justify-between gap-3">
	                      <p className="text-[11px] font-semibold text-gray-700">图文融合</p>
	                      <p className="text-[11px] text-gray-400">合计 100%</p>
	                    </div>
	                    <div className="flex items-center justify-between text-[12px] font-medium text-gray-600">
	                      <span>文本整体 {Math.round(ragTextGroupWeight * 100)}%</span>
	                      <span>图片理解 {Math.round(ragImageVectorWeight * 100)}%</span>
	                    </div>
	                      <input
	                        type="range"
	                        min="0"
                        max="1"
                        step="0.05"
                        value={ragImageVectorWeight}
	                        onChange={(e) => setRagImageVectorWeight(parseFloat(e.target.value))}
	                        className="mt-3 w-full accent-[#002fa7]"
	                      />
	                    <div className="mt-1 flex justify-between text-[10px] text-gray-400">
	                      <span>更偏文本</span>
	                      <span>更偏图片</span>
	                    </div>
	                  </div>
				</div>
				</SettingsCard>
				</section>
				<section id="rag-section-rerank" className="scroll-mt-6">
				<SettingsCard title="重排 Rerank" icon={Filter} color="#002fa7">
                <div className="rounded-xl border border-[#002fa7]/10 bg-white/70 p-3.5 space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="text-[12px] font-semibold text-gray-800">重排 Rerank</p>
                      <p className="mt-0.5 text-[11px] text-gray-500">
                        对召回候选重新排序。开启后，最终结果会优先看重排模型判断。
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => setRagRerankEnabled((value) => !value)}
                      className={`relative h-7 w-12 rounded-full transition ${
                        ragRerankEnabled ? "bg-[#002fa7]" : "bg-gray-200"
                      }`}
                      aria-pressed={ragRerankEnabled}
                    >
                      <span
                        className={`absolute top-1 h-5 w-5 rounded-full bg-white shadow-sm transition ${
                          ragRerankEnabled ? "left-6" : "left-1"
                        }`}
                      />
                    </button>
                  </div>
                  <div className="grid gap-3">
                    <FormField label={`重排候选池: ${ragRerankCandidateTopK}`}>
                      <input
                        type="range"
                        min="10"
                        max="100"
                        step="5"
                        value={ragRerankCandidateTopK}
                        onChange={(e) => setRagRerankCandidateTopK(parseInt(e.target.value))}
                        className="w-full accent-[#002fa7]"
                      />
                    </FormField>
                  </div>
                  <p className="text-[11px] leading-5 text-gray-400">
                    重排后的最终输出数量跟随上面的 Top-K。
                  </p>
                </div>
              </SettingsCard>
			  </section>
			</SettingsAnchorLayout>
            )}

            {/* Core Database Settings */}
            {activeCategory === "database" && (
              <div className="space-y-5">
                <SettingsCard title="核心数据库" icon={Database} color="#0f172a">
                  <div className="rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-3">
                    <p className="text-[11px] leading-relaxed text-slate-600">
                      数据库方案在 CLI 初始化时确定。CLI 负责探测、验证并写入连接配置；Backend 启动后只按配置连接，不负责数据库服务的启动、停止、升级或删除。
                    </p>
                  </div>

                  {databaseEnvOverride ? (
                    <div className="rounded-xl border border-amber-100 bg-amber-50/60 px-3.5 py-3 text-[11px] text-amber-700">
                      <p>当前连接由 CLI 配置提供。这里展示实际生效的信息；更换数据库不需要重新初始化其他配置。</p>
                      <button
                        type="button"
                        onClick={() => {
                          void navigator.clipboard.writeText("puddingclaw database configure");
                          showToast("success", "已复制数据库重配命令");
                        }}
                        className="mt-2 inline-flex items-center gap-1.5 rounded-lg border border-amber-200 bg-white/70 px-2.5 py-1.5 font-medium text-amber-800 hover:bg-white"
                      >
                        <Copy className="h-3.5 w-3.5" />
                        puddingclaw database configure
                      </button>
                    </div>
                  ) : null}

                  <div className="max-w-xl">
                    <FormField label={databaseEnvOverride ? "当前方案" : "存储方式"}>
                      {databaseEnvOverride ? (
                        <div className="form-input flex items-center bg-gray-50 text-gray-700">
                          {databaseMode === "sqlite"
                            ? "SQLite · 本地默认"
                            : databaseSource === "native_apt" || databaseSource === "local"
                              ? "本机 PostgreSQL"
                              : databaseSource === "docker"
                                ? "Docker PostgreSQL"
                                : "外部 PostgreSQL"}
                        </div>
                      ) : (
                        <select
                          value={databaseMode === "sqlite" ? "sqlite" : "external"}
                          onChange={(e) => handleDatabaseModeChange(e.target.value as "sqlite" | "external")}
                          className="form-select"
                        >
                          <option value="sqlite">SQLite · 本地默认（推荐）</option>
                          <option value="external">PostgreSQL · 服务端/共享部署</option>
                        </select>
                      )}
                    </FormField>
                  </div>

                  {databaseMode === "sqlite" ? (
                    <div className="rounded-xl border border-emerald-100 bg-emerald-50/50 px-4 py-3.5">
                      <div className="flex items-start gap-3">
                        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-white text-emerald-700 shadow-sm ring-1 ring-emerald-100">
                          <Database className="h-4.5 w-4.5" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="text-[12px] font-semibold text-slate-800">本地 SQLite Catalog</p>
                            <span className="rounded-full border border-emerald-200 bg-white/80 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                              当前启用
                            </span>
                          </div>
                          <p className="mt-1 text-[11px] leading-relaxed text-slate-600">
                            Core 数据保存在本机单文件中，无需主机、端口、账号或独立数据库服务。
                          </p>
                          <div className="mt-3 rounded-lg border border-emerald-100 bg-white/70 px-3 py-2">
                            <p className="text-[10px] font-medium uppercase tracking-wide text-slate-400">Catalog 文件</p>
                            <p className="mt-1 break-all font-mono text-[11px] text-slate-700">{databaseCatalogPath}</p>
                          </div>
                          <p className="mt-2 text-[10px] leading-relaxed text-slate-500">
                            gbrain / pgvector 与外部业务数据源是独立可选项，不会改变 Core 的存储方式。
                          </p>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="grid gap-4 md:grid-cols-2">
                        <FormField label="主机">
                          <input
                            value={databaseHost}
                            onChange={(e) => setDatabaseHost(e.target.value)}
                            disabled={databaseEnvOverride}
                            className="form-input disabled:cursor-not-allowed disabled:bg-gray-50 disabled:text-gray-400"
                            placeholder="127.0.0.1"
                          />
                        </FormField>
                        <FormField label="端口">
                          <input
                            type="number"
                            min="1"
                            max="65535"
                            value={databasePort}
                            onChange={(e) => setDatabasePort(e.target.value)}
                            disabled={databaseEnvOverride}
                            className="form-input disabled:cursor-not-allowed disabled:bg-gray-50 disabled:text-gray-400"
                            placeholder="5432"
                          />
                        </FormField>
                        <FormField label="数据库名">
                          <input
                            value={databaseName}
                            onChange={(e) => setDatabaseName(e.target.value)}
                            disabled={databaseEnvOverride}
                            className="form-input disabled:cursor-not-allowed disabled:bg-gray-50 disabled:text-gray-400"
                            placeholder="puddingclaw"
                          />
                        </FormField>
                        <FormField label="用户名">
                          <input
                            value={databaseUsername}
                            onChange={(e) => setDatabaseUsername(e.target.value)}
                            disabled={databaseEnvOverride}
                            className="form-input disabled:cursor-not-allowed disabled:bg-gray-50 disabled:text-gray-400"
                            placeholder="puddingclaw"
                          />
                        </FormField>
                        <FormField label="密码">
                          <input
                            type="password"
                            value={databasePassword}
                            onChange={(e) => setDatabasePassword(e.target.value)}
                            disabled={databaseEnvOverride}
                            className="form-input disabled:cursor-not-allowed disabled:bg-gray-50 disabled:text-gray-400"
                            placeholder="留空表示保留已保存的密码"
                          />
                        </FormField>
                      </div>
                      <div className="rounded-xl border border-blue-100 bg-blue-50/50 px-3.5 py-3 text-[11px] leading-relaxed text-blue-700">
                        {databaseSource === "native_apt" || databaseSource === "local"
                        ? "初始化向导通过系统包安装或复用本机 PostgreSQL，创建应用所需的角色与数据库并写入连接信息；后续服务生命周期归用户和操作系统管理。"
                        : databaseSource === "docker"
                          ? "初始化向导创建或复用 Docker PostgreSQL 并写入连接配置；后续服务生命周期由 Docker 管理。"
                          : "初始化向导验证并保存外部 PostgreSQL 的连接信息；Backend 后续只建立连接。"}
                      </div>
                    </>
                  )}

                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={handleTestDatabase}
                      disabled={databaseEnvOverride || databaseTesting}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-gray-950 px-3 py-2 text-[11px] font-medium text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {databaseTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
                      {databaseMode === "sqlite" ? "检查本地 Catalog" : "测试连接"}
                    </button>
                    <span className="text-[11px] text-gray-400">
                      当前来源：{({
                        environment: "CLI Runtime",
                        "config.json": "config.json",
                        default: "内置默认",
                      } as Record<string, string>)[databaseConfiguredBy] || databaseConfiguredBy}
                      {` · ${({
                        local_file: "本地 SQLite",
                        native_apt: "本机 PostgreSQL",
                        local: "本机 PostgreSQL",
                        docker: "Docker",
                        external: "外部 PostgreSQL",
                        fallback: "SQLite",
                      } as Record<string, string>)[databaseSource] || databaseSource}`}
                    </span>
                  </div>
                  {databaseTestResult ? (
                    <div className={`rounded-xl border px-3.5 py-3 text-[11px] ${
                      databaseTestResult.ok
                        ? "border-emerald-100 bg-emerald-50 text-emerald-700"
                        : "border-red-100 bg-red-50 text-red-700"
                    }`}>
                      {databaseTestResult.msg}
                    </div>
                  ) : null}
                </SettingsCard>
              </div>
            )}

            {/* Knowledge Base Settings */}
            {activeCategory === "knowledge" && (
              <SettingsAnchorLayout prefix="knowledge" sections={KNOWLEDGE_SECTIONS}>
                <section id="knowledge-section-directory" className="scroll-mt-6">
                <SettingsCard title="本地知识库目录" icon={FolderOpen} color="#002fa7">
                  <div className="rounded-xl border border-blue-100 bg-blue-50/50 px-3.5 py-3">
                    <p className="text-[11px] leading-relaxed text-blue-700">
                      知识库目录是用户资产目录：PDF 原件、MinerU Markdown、图片 assets、md glob/grep 与 LlamaIndex 索引都会围绕这个目录工作。建议选择 Documents 下的长期目录，而不是项目代码目录。
                    </p>
                  </div>
                  {knowledgeEnvOverride && (
                    <div className="rounded-xl border border-amber-100 bg-amber-50/60 px-3.5 py-3 text-[11px] text-amber-700">
                      当前目录由环境变量 PUDDINGCLAW_KNOWLEDGE_DIR 覆盖；保存 config.json 后不会改变运行时覆盖值。
                    </div>
                  )}
                  <FormField label="知识库根目录">
                    <div className="flex gap-2">
                      <input
                        value={knowledgeRootDir}
                        onChange={(e) => setKnowledgeRootDir(e.target.value)}
                        className="form-input"
                        placeholder="/Users/you/Documents/PuddingClawKnowledge（留空使用默认知识库目录）"
                      />
                      <button
                        type="button"
                        onClick={handleChooseKnowledgeFolder}
                        className="shrink-0 rounded-lg bg-[#002fa7]/10 px-3 py-1.5 text-[11px] font-medium text-[#002fa7] hover:bg-[#002fa7]/15"
                      >
                        选择目录
                      </button>
                    </div>
                  </FormField>
                  <Link
                    href="/knowledge"
                    className="inline-flex items-center gap-1.5 rounded-lg border border-black/[0.06] bg-white px-3 py-2 text-[11px] font-medium text-gray-600 hover:bg-black/[0.02]"
                  >
                    打开知识库管理页
                    <ExternalLink className="h-3.5 w-3.5" />
                  </Link>
                </SettingsCard>
                </section>

                <section id="knowledge-section-wiki" className="scroll-mt-6">
                <SettingsCard title="LLM Wiki" icon={Bot} color="#7c3aed">
                  <div className="flex items-center gap-2">
                    <Bot className="h-4 w-4 text-violet-600" />
                    <p className="text-[12px] font-semibold text-gray-800">编译 Agent</p>
                  </div>
                  <div className="rounded-xl border border-violet-100 bg-violet-50/50 px-3.5 py-3">
                    <p className="text-[11px] leading-relaxed text-violet-700">
                      专门在后台把 Raw 编译成 Wiki。它不进入聊天 Session，只加载 Context、Publish 和 Lint 三个工具；模型接口与密钥仍由「模型服务」统一管理。
                    </p>
                  </div>
                  <FormField label="编译模型">
                    <ModelBindingSelect
                      value={wikiCompilerModelId}
                      onChange={setWikiCompilerModelId}
                      variant="light"
                      options={[
                        { id: "", label: "跟随主 Agent 模型" },
                        ...allProviderModels
                          .filter((model) => model.capability === "llm")
                          .map((model) => ({
                            id: model.id,
                            label: `${model.provider.name} · ${model.name}`,
                          })),
                      ]}
                    />
                    <p className="mt-1 text-[11px] leading-relaxed text-gray-400">
                      新任务会锁定提交时的模型；修改设置不会改变已经排队或正在运行的任务。
                    </p>
                  </FormField>
                  <button
                    type="button"
                    onClick={() => setCategory("ai")}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] font-medium text-slate-700 transition hover:border-[#002fa7]/20 hover:text-[#002fa7]"
                  >
                    管理模型与密钥
                    <ExternalLink className="h-3.5 w-3.5" />
                  </button>

                  <div className="border-t border-black/[0.06] pt-4">
                    <div className="mb-3 flex items-center gap-2">
                      <Search className="h-4 w-4 text-[#002fa7]" />
                      <p className="text-[12px] font-semibold text-gray-800">查询与 Embedding</p>
                    </div>
                  </div>
                  <div className="flex items-center justify-between gap-4 rounded-xl border border-black/[0.06] bg-white/60 px-3.5 py-3">
                    <div className="min-w-0">
                      <p className="text-[12px] font-medium text-gray-800">启用关键词 + Embedding 混合检索</p>
                      <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                        关闭时完全沿用当前 Markdown Query；开启后在同一 LlamaIndex Text Collection 中加入 Wiki 语义召回，并与关键词结果融合。向量不可用时自动回退，不影响 Wiki 查询。
                      </p>
                    </div>
                    <SwitchButton
                      checked={wikiHybridEnabled}
                      onChange={(enabled) => void handleWikiHybridChange(enabled)}
                      ariaLabel="启用 LLM Wiki 混合检索"
                      disabled={wikiHybridSaving}
                    />
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`rounded-full px-2.5 py-1 text-[10px] font-medium ${wikiHybridEnabled ? "bg-emerald-50 text-emerald-700" : "bg-gray-100 text-gray-500"}`}>
                      {wikiHybridEnabled ? "混合检索" : "仅 Markdown Query"}
                    </span>
                    <p className="text-[11px] text-gray-400">
                      {wikiHybridSaving ? "正在保存…" : "开关会自动保存；首次开启请到 Studio 同步已有页面。"}
                    </p>
                  </div>
                  <Link
                    href="/knowledge/schema"
                    className="inline-flex items-center gap-1.5 rounded-lg border border-black/[0.06] bg-white px-3 py-2 text-[11px] font-medium text-gray-600 hover:text-[#002fa7]"
                  >
                    管理 Wiki Embedding
                    <ExternalLink className="h-3.5 w-3.5" />
                  </Link>
                </SettingsCard>
                </section>

                <section id="knowledge-section-gbrain" className="scroll-mt-6">
                  <SettingsCard title="GBrain" icon={Brain} color="#0f766e">
                    <div className="rounded-xl border border-teal-100 bg-teal-50/50 px-3.5 py-3">
                      <p className="text-[11px] leading-relaxed text-teal-700">
                        统一配置 GBrain 的检索模型、Think 模型与独立 PostgreSQL。模型接口和密钥复用「模型服务」；数据库保存 Wiki 页面、关系与向量，不与 PuddingClaw 主数据库混用。
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 text-[11px]">
                      <span className={`rounded-full px-2.5 py-1 font-medium ${gbrainWorkspace?.gbrain.postgres_configured ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>
                        PostgreSQL {gbrainWorkspace?.gbrain.postgres_configured ? "已配置" : "未配置"}
                      </span>
                      <span className={`rounded-full px-2.5 py-1 font-medium ${gbrainWorkspace?.gbrain.cli_installed ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>
                        CLI {gbrainWorkspace?.gbrain.cli_installed ? "已安装" : "未安装"}
                      </span>
                      <span className={`rounded-full px-2.5 py-1 font-medium ${gbrainWorkspace?.gbrain.models.configured ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>
                        模型 {gbrainWorkspace?.gbrain.models.configured ? "已配置" : "未配置"}
                      </span>
                    </div>
                    <div className="border-t border-black/[0.06] pt-4">
                      <p className="mb-3 text-[12px] font-semibold text-gray-800">检索与推理模型</p>
                    </div>
                  <FormField label="Embedding 模型">
                    <ModelBindingSelect
                      value={wikiGbrainEmbeddingModelId}
                      onChange={setWikiGbrainEmbeddingModelId}
                      variant="light"
                      options={[
                        { id: "", label: "跟随文本 Embedding 模型" },
                        ...allProviderModels
                          .filter((model) => model.capability === "text_embedding")
                          .map((model) => ({
                            id: model.id,
                            label: `${model.provider.name} · ${model.name}${model.dimension ? ` · ${model.dimension} 维` : ""}`,
                          })),
                      ]}
                    />
                    <p className="mt-1 text-[11px] leading-relaxed text-gray-400">
                      初始化 PostgreSQL Brain 时固定向量维度；更换后需重新初始化或迁移 Embedding。
                    </p>
                  </FormField>
                  <FormField label="Think 模型">
                    <ModelBindingSelect
                      value={wikiGbrainThinkModelId}
                      onChange={setWikiGbrainThinkModelId}
                      variant="light"
                      options={[
                        { id: "", label: "跟随主 Agent 模型" },
                        ...allProviderModels
                          .filter((model) => model.capability === "llm")
                          .map((model) => ({
                            id: model.id,
                            label: `${model.provider.name} · ${model.name}`,
                          })),
                      ]}
                    />
                    <p className="mt-1 text-[11px] leading-relaxed text-gray-400">
                      用于 GBrain Think 的多跳综合；可独立调整，不要求重建向量。
                    </p>
                  </FormField>
                  <button
                    type="button"
                    onClick={() => setCategory("ai")}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] font-medium text-slate-700 transition hover:border-[#002fa7]/20 hover:text-[#002fa7]"
                  >
                    管理模型与密钥
                    <ExternalLink className="h-3.5 w-3.5" />
                  </button>

                    <div className="border-t border-black/[0.06] pt-4">
                      <div className="mb-3">
                        <p className="text-[12px] font-semibold text-gray-800">PostgreSQL 数据库</p>
                        <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                          复用本机 PostgreSQL 服务，但为 GBrain 使用独立 database。Studio 只执行预检和入库，不再维护数据库连接。
                        </p>
                      </div>
                      <div className="grid gap-4 md:grid-cols-2">
                        <FormField label="模式">
                          <select value="external" disabled className="form-select disabled:cursor-not-allowed disabled:bg-gray-50 disabled:text-gray-500">
                            <option value="external">本机 PostgreSQL</option>
                          </select>
                        </FormField>
                        <FormField label="本机端口">
                          <input
                            type="number"
                            min="1"
                            max="65535"
                            value={gbrainDatabasePort}
                            onChange={(event) => setGbrainDatabasePort(event.target.value)}
                            className="form-input"
                            placeholder="5432"
                          />
                        </FormField>
                        <FormField label="数据库名">
                          <input
                            value={gbrainDatabaseName}
                            onChange={(event) => setGbrainDatabaseName(event.target.value)}
                            className="form-input"
                            placeholder="llm_wiki"
                          />
                        </FormField>
                        <FormField label="用户名">
                          <input
                            value={gbrainDatabaseUsername}
                            onChange={(event) => setGbrainDatabaseUsername(event.target.value)}
                            className="form-input"
                            placeholder="pet"
                          />
                        </FormField>
                        <FormField label="密码">
                          <input
                            type="password"
                            value={gbrainDatabasePassword}
                            onChange={(event) => setGbrainDatabasePassword(event.target.value)}
                            className="form-input"
                            placeholder={gbrainWorkspace?.gbrain.postgres_configured ? "重新配置时输入数据库密码" : "数据库密码（本机免密可留空）"}
                            autoComplete="new-password"
                          />
                        </FormField>
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => void handleTestGbrainDatabase()}
                        disabled={gbrainDatabaseTesting || gbrainInitializing || !gbrainDatabaseName.trim() || !gbrainDatabaseUsername.trim()}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-gray-950 px-3 py-2 text-[11px] font-medium text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {gbrainDatabaseTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
                        测试连接
                      </button>
                      <button
                        type="button"
                        onClick={() => void handleInitializeGbrain()}
                        disabled={gbrainDatabaseTesting || gbrainInitializing || !gbrainDatabaseName.trim() || !gbrainDatabaseUsername.trim() || !gbrainWorkspace?.gbrain.cli_installed || !gbrainWorkspace?.gbrain.models.configured}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-[#002fa7] px-3 py-2 text-[11px] font-medium text-white hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        {gbrainInitializing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Database className="h-3.5 w-3.5" />}
                        {gbrainWorkspace?.gbrain.postgres_configured ? "重新连接并初始化" : "连接并初始化"}
                      </button>
                      {gbrainWorkspace?.gbrain.postgres?.configured ? (
                        <span className="text-[11px] text-gray-400">
                          当前：{gbrainWorkspace.gbrain.postgres.username}@{gbrainWorkspace.gbrain.postgres.host}:{gbrainWorkspace.gbrain.postgres.port}/{gbrainWorkspace.gbrain.postgres.database}
                        </span>
                      ) : null}
                    </div>

                    {!gbrainWorkspace?.gbrain.models.configured ? (
                      <p className="rounded-xl border border-amber-100 bg-amber-50/60 px-3.5 py-3 text-[11px] leading-relaxed text-amber-700">
                        连接并初始化前，请先选择上方的 Embedding 与 Think 模型并保存设置。
                      </p>
                    ) : null}

                    {gbrainDatabaseTestResult ? (
                      <div className={`rounded-xl border px-3.5 py-3 text-[11px] ${gbrainDatabaseTestResult.ok ? "border-emerald-100 bg-emerald-50 text-emerald-700" : "border-red-100 bg-red-50 text-red-700"}`}>
                        {gbrainDatabaseTestResult.msg}
                      </div>
                    ) : null}
                  </SettingsCard>
                </section>

                <section id="knowledge-section-embedding" className="scroll-mt-6">
                <SettingsCard title="多模态 Embedding" icon={Database} color="#002fa7">
                  <div className="rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="text-[12px] font-medium text-slate-700">
                          {multimodalEmbeddingSelection
                            ? `${multimodalEmbeddingSelection.provider.name} · ${multimodalEmbeddingSelection.model.name}`
                            : "尚未绑定多模态 Embedding 模型"}
                        </p>
                        <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
                          {multimodalEmbeddingSelection?.model.dimension
                            ? `${multimodalEmbeddingSelection.model.dimension} 维 · 模型、接口和密钥由「模型服务」统一管理`
                            : "模型、维度、接口和密钥由「模型服务」统一管理"}
                        </p>
                      </div>
                      <button
                        type="button"
                        onClick={() => setCategory("ai")}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] font-medium text-slate-700 transition hover:border-[#002fa7]/20 hover:text-[#002fa7]"
                      >
                        前往模型服务
                        <ExternalLink className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                  <div className="max-w-sm">
                    <FormField label="多模态批量数">
                      <input value={mmBatchSize} onChange={(e) => setMmBatchSize(e.target.value)} className="form-input" placeholder="10" />
                      <p className="mt-1 text-[11px] leading-relaxed text-gray-400">
                        qwen3-vl-embedding 单次最多处理 20 条文本或 10 张图片。
                      </p>
                    </FormField>
                  </div>
                </SettingsCard>
                </section>

                <section id="knowledge-section-index" className="scroll-mt-6">
                <SettingsCard title="知识库检索索引" icon={Database} color="#10b981">
                  <div className="flex items-center justify-between gap-4 rounded-lg border border-black/[0.06] bg-white/50 px-3.5 py-3">
                    <div>
                      <p className="text-[12px] font-medium text-gray-700">让知识库支持语义搜索和图文检索</p>
                      <p className="mt-0.5 text-[11px] text-gray-500">建议保持开启。关闭后只保留本地文件检索。</p>
                    </div>
                    <SwitchButton checked={kbIndexEnabled} onChange={setKbIndexEnabled} ariaLabel="启用知识库多模态索引" />
                  </div>
                  <div className="grid gap-4 md:grid-cols-2">
                    <FormField label="索引服务地址">
                      <input value={kbMilvusUri} onChange={(e) => setKbMilvusUri(e.target.value)} className="form-input" />
                    </FormField>
                    <div className="flex items-end">
                      <p className="pb-2 text-[11px] leading-relaxed text-gray-500">
                        默认使用本机 Milvus：<span className="font-medium text-gray-700">http://localhost:19530</span>
                      </p>
                    </div>
                    <details className="md:col-span-2 rounded-xl border border-black/[0.06] bg-white/60 p-3">
                      <summary className="cursor-pointer text-[11px] font-medium text-gray-600">
                        高级选项
                      </summary>
                      <div className="mt-3 grid gap-4 md:grid-cols-2">
                        <FormField label="索引存储">
                          <select value={kbVectorStore} onChange={(e) => setKbVectorStore(e.target.value)} className="form-select">
                            <option value="milvus">Milvus</option>
                            <option value="local">本地 LlamaIndex 存储</option>
                          </select>
                        </FormField>
                        <FormField label="文本索引名称">
                          <input value={kbTextCollection} onChange={(e) => setKbTextCollection(e.target.value)} className="form-input" />
                        </FormField>
                        <FormField label="图片索引名称">
                          <input value={kbImageCollection} onChange={(e) => setKbImageCollection(e.target.value)} className="form-input" />
                        </FormField>
                      </div>
                    </details>
                    <div className="md:col-span-2 rounded-xl border border-red-100 bg-red-50/50 p-3">
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                        <div>
                          <p className="text-[12px] font-medium text-red-700">清空知识库索引</p>
                          <p className="mt-1 text-[11px] leading-relaxed text-red-600/80">
                            删除已生成的搜索缓存。不会删除你上传的文件；下次使用时会重新生成。
                          </p>
                          <p className="mt-1 text-[11px] text-red-600/70">
                            不确定时不用点它。
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={handleResetVectorCollections}
                          disabled={saving}
                          className="shrink-0 rounded-lg bg-red-600 px-3 py-2 text-[11px] font-semibold text-white transition hover:bg-red-700 disabled:cursor-wait disabled:opacity-60"
                        >
                          立即清空
                        </button>
                      </div>
                    </div>
                  </div>
                </SettingsCard>
                </section>
              </SettingsAnchorLayout>
            )}

            {/* Agent / Harness Config */}
            {activeCategory === "harness" && (
              <div className="flex flex-col lg:flex-row gap-5">
                {/* Left: harness category sidebar */}
                <aside className="flex w-full flex-col gap-4 lg:sticky lg:top-6 lg:w-56 lg:self-start lg:shrink-0">
                  <div>
                    <input
                      type="text"
                      value={harnessFilter}
                      onChange={(e) => setHarnessFilter(e.target.value)}
                      placeholder="筛选分类..."
                      className="form-input text-[12px]"
                    />
                  </div>

                  <div className="flex flex-col gap-1">
                    {filteredHarnessSections.map((section) => {
                      const Icon = section.icon;
                      const active = activeHarnessSection === section.id;
                      return (
                        <button
                          key={section.id}
                          type="button"
                          onClick={() => !section.disabled && scrollToHarnessSection(section.id)}
                          disabled={section.disabled}
                          className={`flex w-full items-start gap-2.5 rounded-xl px-3 py-2.5 text-left transition-all ${
                            section.disabled
                              ? "cursor-not-allowed opacity-50"
                              : active
                                ? "bg-[#002fa7]/[0.07] text-[#002fa7]"
                                : "text-gray-600 hover:bg-black/[0.035] hover:text-gray-900"
                          }`}
                        >
                          <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                          <div className="min-w-0">
                            <p className="text-[12px] font-semibold">{section.label}</p>
                            <p className="truncate text-[10px] opacity-70">{section.description}</p>
                          </div>
                        </button>
                      );
                    })}
                    {filteredHarnessSections.length === 0 && (
                      <p className="px-3 py-2 text-[11px] text-gray-400">无匹配分类</p>
                    )}
                  </div>
                </aside>

                {/* Right: anchored content */}
                <div className="min-w-0 flex-1 space-y-6">
                  <section id="harness-section-subagent" className="scroll-mt-6">
                    <SettingsCard title="SubAgent 配置" icon={Bot} color="#002fa7">
                      <div className="flex flex-col lg:flex-row gap-5 min-h-[480px]">
                        {/* Left column: subagent list */}
                        <div className="w-full lg:w-[38%] flex flex-col gap-3">
                          <div className="flex-1 space-y-2.5 overflow-y-auto max-h-[560px] pr-1">
                            {subagentItems.length === 0 && (
                              <div className="rounded-xl border border-dashed border-black/[0.08] bg-white/50 p-6 text-center text-[12px] text-gray-400">
                                暂无 SubAgent，点击下方按钮添加
                              </div>
                            )}
                            {subagentItems.map((item, index) => {
                              const selected = selectedSubagentIndex === index;
                              const valid = isSubAgentValid(item);
                              return (
                                <div
                                  key={index}
                                  onClick={() => setSelectedSubagentIndex(index)}
                                  className={`group flex items-center gap-3 rounded-xl border p-3 cursor-pointer transition-all ${
                                    selected
                                      ? "border-[#002fa7] bg-[#002fa7]/[0.04] shadow-sm"
                                      : "border-black/[0.06] bg-white hover:border-[#002fa7]/30 hover:bg-[#002fa7]/[0.02]"
                                  }`}
                                >
                                  <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl shadow-sm ${
                                    selected ? "bg-[#002fa7] text-white" : "bg-white text-[#002fa7]"
                                  }`}>
                                    <Bot className="h-4 w-4" />
                                  </div>
                                  <div className="flex-1 min-w-0">
                                    <p className="text-[13px] font-semibold text-gray-800">SubAgent #{index + 1}</p>
                                    <p className="text-[11px] text-gray-500 truncate">{item.name || "未命名子代理"}</p>
                                  </div>
                                  <div className="flex items-center gap-2">
                                    {!valid && !item.enabled && (
                                      <span className="rounded-full bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-600">
                                        未完善
                                      </span>
                                    )}
                                    <SwitchButton
                                      checked={item.enabled}
                                      onChange={(checked) => handleToggleSubAgent(index, checked)}
                                      ariaLabel={`启用 ${item.name || "SubAgent"} 子代理`}
                                    />
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                          <div className="flex items-center gap-2">
                            <button
                              type="button"
                              onClick={handleAddSubAgent}
                              className="flex-1 rounded-xl border border-dashed border-[#002fa7]/30 bg-[#002fa7]/[0.02] py-2.5 text-[12px] font-medium text-[#002fa7] hover:bg-[#002fa7]/[0.05] transition-colors"
                            >
                              + 新增 SubAgent
                            </button>
                            <button
                              type="button"
                              onClick={() => setConfigModalOpen(true)}
                              className="flex items-center gap-1.5 rounded-xl border border-black/[0.06] bg-white px-3 py-2.5 text-[11px] font-medium text-gray-600 hover:bg-black/[0.02] transition-colors"
                            >
                              <Braces className="h-3.5 w-3.5" />
                              查看 config.json
                            </button>
                          </div>
                        </div>

                        {/* Right column: desktop editor */}
                        <div className="hidden lg:block w-[62%]">
                          {selectedSubagentIndex !== null && subagentItems[selectedSubagentIndex] ? (
                            <SubAgentEditorPanel
                              index={selectedSubagentIndex}
                              item={subagentItems[selectedSubagentIndex]}
                              agentModels={agentModels}
                              imageAnalyzerModels={imageAnalyzerProviderModels
                                .map((model) => ({ id: model.id, name: model.name, providerName: model.provider.name }))}
                              imageAnalyzerModelId={imageAnalyzerBoundId}
                              refreshingModels={refreshingModels}
                              onChange={updateSubAgentItem}
                              onRefreshModels={handleRefreshAgentModels}
                              onBindImageAnalyzer={(modelId) => handleBindProvider("image_analyzer", modelId)}
                              onDelete={handleDeleteSubAgent}
                              onClose={() => setSelectedSubagentIndex(null)}
                            />
                          ) : (
                            <div className="flex h-full min-h-[360px] flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-black/[0.08] bg-white/50 p-6 text-center">
                              <Bot className="h-8 w-8 text-gray-300" />
                              <p className="text-[12px] text-gray-400">选择一个 SubAgent 以编辑配置</p>
                            </div>
                          )}
                        </div>
                      </div>
                    </SettingsCard>
                  </section>

                  <section id="harness-section-context" className="scroll-mt-6">
                    <SettingsCard title="上下文工程" icon={Brain} color="#002fa7">
                      <div className="space-y-4">
                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="mb-4">
                            <p className="text-[13px] font-semibold text-gray-900">DeepAgents 全局摘要</p>
                            <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                              自动摘要与手动 /compact 共用独立模型；摘要输入预算由后端根据当前模型上下文窗口自动计算。
                            </p>
                          </div>
                          <FormField label="摘要 / Compact 模型">
                            <ModelBindingSelect
                              value={contextSummaryModelId}
                              onChange={setContextSummaryModelId}
                              variant="light"
                              options={[
                                { id: "", label: "跟随当前 Agent 模型" },
                                ...allProviderModels
                                  .filter((model) => model.capability === "llm" && model.provider.enabled)
                                  .map((model) => ({
                                    id: model.id,
                                    label: `${model.provider.name} · ${model.name}`,
                                  })),
                              ]}
                            />
                            <p className="mt-1 text-[10px] leading-relaxed text-gray-400">
                              推荐选择低延迟模型，例如 DeepSeek V4 Flash。该选择不会改变 Session 的主 Agent 模型。
                            </p>
                          </FormField>
                          <FormField label="全局摘要触发阈值（tokens）">
                            <input
                              type="number"
                              min={10000}
                              max={1000000}
                              step={10000}
                              value={contextSummaryTriggerTokens}
                              onChange={(e) => setContextSummaryTriggerTokens(e.target.value)}
                              className="form-input"
                            />
                          </FormField>
                          <FormField label="摘要后原始上下文保留预算（tokens）">
                            <input
                              type="number"
                              min={1000}
                              max={999999}
                              step={1000}
                              value={contextSummaryKeepTokens}
                              onChange={(e) => setContextSummaryKeepTokens(e.target.value)}
                              className="form-input"
                            />
                            <p className="mt-1 text-[10px] leading-relaxed text-gray-400">
                              默认 64,000 tokens；必须低于触发阈值。旧消息会进入摘要，最近的原始上下文保留在此预算内。
                            </p>
                          </FormField>
                        </div>

                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <div>
                              <p className="text-[13px] font-semibold text-gray-900">工具上下文压缩</p>
                              <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                                管理历史 Tool Result 的后台摘要，并控制是否允许开启当前轮即时压缩。
                              </p>
                            </div>
                            <SwitchButton
                              checked={toolContextEnabled}
                              onChange={setToolContextEnabled}
                              ariaLabel="启用工具上下文压缩"
                            />
                          </div>
                          {!toolContextEnabled && (
                            <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-[10px] leading-relaxed text-amber-700">
                              工具上下文中间件将不注册；DeepAgents 的 200k 全局摘要仍然生效。
                            </p>
                          )}

                          <div className="mt-3 rounded-lg border border-blue-100 bg-blue-50/70 px-3 py-2 text-[10px] leading-relaxed text-blue-700">
                            PuddingClaw 统一接管单条文本 Tool Result 超过 20,000 tokens 的无损落盘：
                            完整内容写入 <code>/large_tool_results/</code>，Agent 只接收预览和精确读取路径。
                            该机制不受工具类型影响，并且始终生效，不依赖下面的可选即时压缩。
                          </div>

                          <div className="mt-3 rounded-lg border border-black/[0.05] bg-white/70 px-3 py-3">
                            <div className="flex items-center justify-between gap-4">
                              <div>
                                <p className="text-[11px] font-semibold text-gray-800">执行中单条即时压缩</p>
                                <p className="mt-0.5 text-[10px] leading-relaxed text-gray-400">
                                  可选。达到阈值时先裁成 head/tail；默认关闭，优先保留当前轮完整证据。
                                </p>
                              </div>
                              <SwitchButton
                                checked={immediateToolCompactionEnabled}
                                onChange={setImmediateToolCompactionEnabled}
                                ariaLabel="启用执行中单条即时压缩"
                                disabled={!toolContextEnabled}
                              />
                            </div>
                            <div className="mt-3 border-t border-black/[0.05] pt-3">
                              <FormField label="即时压缩阈值">
                                <input
                                  type="number"
                                  min={1000}
                                  max={20000}
                                  step={1000}
                                  value={singleToolTriggerTokens}
                                  onChange={(e) => setSingleToolTriggerTokens(e.target.value)}
                                  className="form-input"
                                  disabled={!toolContextEnabled || !immediateToolCompactionEnabled}
                                />
                                <p className="mt-1 text-[10px] text-gray-400">
                                  1,000–20,000 tokens。不得超过 20,000；更大的结果会先被无损落盘。
                                </p>
                              </FormField>
                            </div>
                          </div>

                          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
                            <FormField label="静默压缩单条下限">
                              <input
                                type="number"
                                min={100}
                                step={100}
                                value={backgroundMinResultTokens}
                                onChange={(e) => setBackgroundMinResultTokens(e.target.value)}
                                className="form-input"
                                disabled={!toolContextEnabled}
                              />
                              <p className="mt-1 text-[10px] text-gray-400">默认 1,000 tokens；更短结果保持原样。</p>
                            </FormField>
                            <FormField label="完整 Tool Context 预算">
                              <input
                                type="number"
                                min={1000}
                                max={500000}
                                step={1000}
                                value={retainToolContextTokens}
                                onChange={(e) => setRetainToolContextTokens(e.target.value)}
                                className="form-input"
                                disabled={!toolContextEnabled}
                              />
                              <p className="mt-1 text-[10px] text-gray-400">
                                默认保留最近 32,000 tokens；单个结果超过 20,000 tokens 时会无损落盘。
                              </p>
                            </FormField>
                          </div>
                        </div>

                        <SpecPreview
                          spec={{
                            compression: {
                              deepagents: {
                                summarization: {
                                  model_id: contextSummaryModelId,
                                  trigger_tokens: positiveIntOrNull(contextSummaryTriggerTokens) ?? 272000,
                                  keep_tokens: positiveIntOrNull(contextSummaryKeepTokens) ?? 64000,
                                },
                                tool_context: {
                                  enabled: toolContextEnabled,
                                  immediate_compaction_enabled: immediateToolCompactionEnabled,
                                  single_tool_trigger_tokens: positiveIntOrNull(singleToolTriggerTokens) ?? 8000,
                                  background_min_result_tokens: positiveIntOrNull(backgroundMinResultTokens) ?? 1000,
                                  retain_tool_context_tokens: positiveIntOrNull(retainToolContextTokens) ?? 32000,
                                },
                              },
                            },
                          }}
                        />
                      </div>
                    </SettingsCard>
                  </section>

                  <section id="harness-section-prompt-cache" className="scroll-mt-6">
                    <SettingsCard title="Prompt 缓存" icon={Braces} color="#002fa7">
                      <div className="space-y-4">
                        <div className="rounded-xl border border-blue-100 bg-blue-50/60 px-3.5 py-3 text-[11px] leading-relaxed text-blue-800">
                          Provider 通常按请求前缀复用 Prompt Cache。这里控制系统提示词、消息历史和工具
                          Schema 是否以稳定、可诊断的形式发送。它只优化模型输入，不改变 Skill、权限或 Tool Gate 的权威判断。
                        </div>

                        <div className="grid gap-3 lg:grid-cols-2">
                          <ToggleRow
                            label="系统提示词分区排序"
                            description="按 Stable Core → Project → Semantics → Memory → Active Runtime → Volatile Tail 固定排列，减少中间区块变化造成的缓存失效。"
                            checked={orderedSystemSections}
                            onChange={setOrderedSystemSections}
                          />
                          <ToggleRow
                            label="尾部路由控制消息"
                            description="保持用户原文不变，把 Skill 路由和能力建议合并为请求级临时尾消息；该消息不会写入 Session。"
                            checked={tailRoutingMessage}
                            onChange={setTailRoutingMessage}
                          />
                          <ToggleRow
                            label="确定性 Session 消息投影"
                            description="保持历史消息边界和顺序稳定，不再合并连续 Assistant 消息，有利于缓存比较和 Tool Call 协议恢复。"
                            checked={deterministicSessionProjection}
                            onChange={setDeterministicSessionProjection}
                          />
                          <ToggleRow
                            label="稳定工具 Schema"
                            description="发送已挂载工具的有界稳定超集并固定排序；Schema 可见不等于获得调用权限，实际调用仍由 Tool Gate 拦截。"
                            checked={stableToolSchema}
                            onChange={setStableToolSchema}
                          />
                        </div>

                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <div>
                              <p className="text-[12px] font-semibold text-gray-800">缓存分段诊断</p>
                              <p className="mt-1 text-[10px] leading-relaxed text-gray-500">
                                在 Trace 中记录 system、tools、messages 各部分指纹及首个变化位置，便于判断缓存为什么命中或失效；不改变发送内容。
                              </p>
                            </div>
                            <SwitchButton
                              checked={tracePartDiagnostics}
                              onChange={setTracePartDiagnostics}
                              ariaLabel="启用 Prompt 缓存分段诊断"
                            />
                          </div>
                        </div>

                        {stableToolSchema && (
                          <p className="rounded-lg bg-amber-50 px-3 py-2 text-[10px] leading-relaxed text-amber-700">
                            稳定工具 Schema 可能增加每轮输入 token，并受 Provider 工具数量与 Schema 大小限制。建议结合缓存分段诊断按模型灰度验证。
                          </p>
                        )}

                        <SpecPreview
                          spec={{
                            harness: {
                              prompt_cache: {
                                trace_part_diagnostics: tracePartDiagnostics,
                                ordered_system_sections: orderedSystemSections,
                                tail_routing_message: tailRoutingMessage,
                                deterministic_session_projection: deterministicSessionProjection,
                                stable_tool_schema: stableToolSchema,
                              },
                            },
                          }}
                        />
                      </div>
                    </SettingsCard>
                  </section>

                  <section id="harness-section-completion" className="scroll-mt-6">
                    <SettingsCard title="Goal 与验收" icon={Target} color="#002fa7">
                      <div className="space-y-4">
                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <div>
                              <p className="text-[13px] font-semibold text-gray-900">显式 Goal Mode</p>
                              <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                                Goal 默认关闭，只有用户在输入区主动勾选才创建。系统不会因任务复杂或 Run 失败自动升级。
                              </p>
                            </div>
                            <SwitchButton
                              checked={goalsEnabled}
                              onChange={setGoalsEnabled}
                              ariaLabel="允许用户开启 Goal Mode"
                            />
                          </div>
                          <div className="mt-4 max-w-xs">
                            <FormField label="单 Goal 最大 Run 数">
                              <input
                                type="number"
                                min={1}
                                max={100}
                                value={goalMaxRounds}
                                onChange={(event) => setGoalMaxRounds(event.target.value)}
                                className="form-input"
                                disabled={!goalsEnabled}
                              />
                              <p className="mt-1 text-[10px] leading-relaxed text-gray-400">
                                当前 Goal 总预算按跨 Run 轮数控制；模型调用次数限制是单 Run
                                的即时熔断器，不等同于 Goal token 预算。
                              </p>
                            </FormField>
                          </div>
                        </div>

                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <div>
                              <p className="text-[13px] font-semibold text-gray-900">Rubric 验收 <span className="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-800">实验性</span></p>
                              <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                                开启后，新建 Goal 统一使用独立模型复核；关闭时，新建 Goal 默认采用标准验收。
                              </p>
                            </div>
                            <SwitchButton
                              checked={rubricEnabled}
                              onChange={setRubricEnabled}
                              ariaLabel="启用 Goal Run Rubric"
                            />
                          </div>
                          {rubricEnabled ? <div className="mt-4 grid max-w-xl gap-3 sm:grid-cols-2">
                            <FormField label="最大 Rubric 尝试次数">
                              <input
                                type="number"
                                min={1}
                                max={20}
                                value={rubricMaxIterations}
                                onChange={(event) => setRubricMaxIterations(event.target.value)}
                                className="form-input"
                                disabled={!rubricEnabled}
                              />
                              <p className="mt-1 text-[10px] leading-relaxed text-gray-400">
                                每次完成申请最多允许的独立验收次数。
                              </p>
                            </FormField>
                            <FormField label="相同缺口最多自动修复次数">
                              <input
                                type="number"
                                min={1}
                                max={20}
                                value={rubricMaxStagnantRepairs}
                                onChange={(event) => setRubricMaxStagnantRepairs(event.target.value)}
                                className="form-input"
                                disabled={!rubricEnabled}
                              />
                              <p className="mt-1 text-[10px] leading-relaxed text-gray-400">
                                同一组可修复缺口连续没有变化时，最多再尝试 N 次；达到阈值后停止，避免无效循环。控制面或验证器故障会立即停止，不消耗该次数。
                              </p>
                            </FormField>
                          </div> : null}
                        </div>

                        {rubricEnabled && <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <div>
                              <p className="text-[13px] font-semibold text-gray-900">高级自定义验收规则</p>
                              <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                                可追加或强化规则，不能关闭系统的数据正确性、安全和证据底线。
                              </p>
                            </div>
                            <SwitchButton
                              checked={customRubricRulesEnabled}
                              onChange={setCustomRubricRulesEnabled}
                              ariaLabel="启用高级自定义 Rubric 规则"
                            />
                          </div>
                          {customRubricRulesEnabled && (
                            <div className="mt-4 space-y-3">
                              {customRubricRules.map((rule, index) => (
                                <div key={rule.id || index} className="rounded-xl border border-black/[0.07] bg-white p-3">
                                  <div className="flex items-start gap-2">
                                    <textarea
                                      value={rule.statement}
                                      onChange={(event) =>
                                        setCustomRubricRules((current) =>
                                          current.map((item, itemIndex) =>
                                            itemIndex === index
                                              ? { ...item, statement: event.target.value }
                                              : item
                                          )
                                        )
                                      }
                                      className="form-input min-h-20 flex-1 resize-y"
                                      placeholder="例如：主要原因必须给出贡献量或影响量级"
                                    />
                                    <button
                                      type="button"
                                      onClick={() =>
                                        setCustomRubricRules((current) =>
                                          current.filter((_, itemIndex) => itemIndex !== index)
                                        )
                                      }
                                      className="rounded-lg p-2 text-gray-400 hover:bg-red-50 hover:text-red-600"
                                      title="删除规则"
                                    >
                                      <X className="h-4 w-4" />
                                    </button>
                                  </div>
                                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                                    <FormField label="验证器">
                                      <select
                                        value={rule.verifier}
                                        onChange={(event) =>
                                          setCustomRubricRules((current) =>
                                            current.map((item, itemIndex) =>
                                              itemIndex === index
                                                ? {
                                                    ...item,
                                                    verifier: event.target.value as
                                                      | "analytics"
                                                      | "llm_grader",
                                                  }
                                                : item
                                            )
                                          )
                                        }
                                        className="form-select"
                                      >
                                        <option value="llm_grader">LLM Grader</option>
                                        <option value="analytics">Analytics Check</option>
                                      </select>
                                    </FormField>
                                    <label className="flex items-center gap-2 pt-6 text-[12px] text-gray-600">
                                      <input
                                        type="checkbox"
                                        checked={rule.required}
                                        onChange={(event) =>
                                          setCustomRubricRules((current) =>
                                            current.map((item, itemIndex) =>
                                              itemIndex === index
                                                ? { ...item, required: event.target.checked }
                                                : item
                                            )
                                          )
                                        }
                                      />
                                      required
                                    </label>
                                  </div>
                                </div>
                              ))}
                              <button
                                type="button"
                                onClick={() =>
                                  setCustomRubricRules((current) => [
                                    ...current,
                                    {
                                      id: `custom_${Date.now()}`,
                                      enabled: true,
                                      statement: "",
                                      required: true,
                                      verifier: "llm_grader",
                                    },
                                  ])
                                }
                                className="rounded-xl border border-dashed border-[#002fa7]/30 px-3 py-2 text-[12px] font-medium text-[#002fa7] hover:bg-[#002fa7]/[0.04]"
                              >
                                + 新增验收规则
                              </button>
                            </div>
                          )}
                        </div>}
                      </div>
                    </SettingsCard>
                  </section>

                  <section id="harness-section-sandbox" className="scroll-mt-6">
                    <SettingsCard title="终端执行" icon={Box} color="#002fa7">
                      <div className="space-y-4">
                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div>
                            <p className="text-[13px] font-semibold text-gray-900">执行模式</p>
                            <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                              Grant Profile 与权限卡保持一致；这里只决定由哪个隔离执行层承载命令。
                            </p>
                            <div className="mt-3 grid gap-2 lg:grid-cols-2">
                              {([
                                ["spawn", "宿主执行", "兼容性优先。命令在当前用户的宿主环境中运行，仍受 Tool Gate 和权限审批约束。"],
                                [
                                  "kernel",
                                  "智能内核沙箱",
                                  "安全性优先。按命令和授权生成最小 OS 级文件、网络与运行时边界。",
                                ],
                              ] as const).map(([value, label, description]) => (
                                <button
                                  key={value}
                                  type="button"
                                  onClick={() => setExecutionMode(value)}
                                  className={`rounded-xl border px-3 py-3 text-left transition ${executionMode === value ? "border-[#002fa7] bg-blue-50/70 ring-1 ring-[#002fa7]/20" : "border-black/[0.07] bg-white hover:bg-slate-50"}`}
                                >
                                  <span className="block text-[12px] font-semibold text-slate-900">{label}</span>
                                  <span className="mt-1 block text-[10px] leading-4 text-slate-500">{description}</span>
                                </button>
                              ))}
                            </div>
                          </div>

                          <p className="mt-3 rounded-xl bg-slate-50 px-3 py-2 text-[11px] leading-5 text-slate-600">
                            宿主执行提供最高兼容性；内核沙箱提供 OS 级最小权限边界。两种模式都继续经过 Tool Gate、Grant 和审计流程。
                          </p>
                        </div>
                        <p className="rounded-xl bg-amber-50 px-3 py-2 text-[11px] leading-5 text-amber-800">
                          当前模式会从下一次 Agent 运行开始生效；内核沙箱不可用时不会静默降级为宿主执行。
                        </p>
                      </div>
                    </SettingsCard>
                  </section>

                  <section id="harness-section-runtime" className="scroll-mt-6">
                    <SettingsCard title="运行保护" icon={ShieldCheck} color="#002fa7">
                      <div className="grid gap-4 lg:grid-cols-[1fr_0.9fr]">
                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <div>
                              <p className="text-[13px] font-semibold text-gray-900">ModelCallLimitMiddleware</p>
                              <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                                限制模型调用次数，防止 Agent 工具循环或模型自我续写导致无限运行。
                              </p>
                            </div>
                            <SwitchButton
                              checked={modelCallLimitEnabled}
                              onChange={setModelCallLimitEnabled}
                              ariaLabel="启用模型调用次数限制"
                            />
                          </div>

                          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
                            <FormField label="单次运行上限">
                              <input
                                type="number"
                                min={1}
                                value={modelCallRunLimit}
                                onChange={(e) => setModelCallRunLimit(e.target.value)}
                                className="form-input"
                                disabled={!modelCallLimitEnabled}
                              />
                              <p className="mt-1 text-[10px] text-gray-400">对应官方 run_limit，默认 50。</p>
                            </FormField>
                            <FormField label="线程累计上限">
                              <input
                                type="number"
                                min={1}
                                value={modelCallThreadLimit}
                                onChange={(e) => setModelCallThreadLimit(e.target.value)}
                                className="form-input"
                                placeholder="不限制"
                                disabled={!modelCallLimitEnabled}
                              />
                              <p className="mt-1 text-[10px] text-gray-400">对应 thread_limit，留空表示不限制。</p>
                            </FormField>
                            <FormField label="触发后行为">
                              <select
                                value={modelCallExitBehavior}
                                onChange={(e) => setModelCallExitBehavior(e.target.value === "error" ? "error" : "end")}
                                className="form-select"
                                disabled={!modelCallLimitEnabled}
                              >
                                <option value="end">优雅结束</option>
                                <option value="error">抛出错误</option>
                              </select>
                              <p className="mt-1 text-[10px] text-gray-400">推荐 end，命中后注入限制消息并结束。</p>
                            </FormField>
                          </div>
                        </div>
                        <SpecPreview
                          spec={{
                            harness: {
                              model_call_limit: {
                                enabled: modelCallLimitEnabled,
                                run_limit: positiveIntOrNull(modelCallRunLimit) ?? 50,
                                thread_limit: positiveIntOrNull(modelCallThreadLimit),
                                exit_behavior: modelCallExitBehavior,
                              },
                            },
                          }}
                        />
                      </div>
                    </SettingsCard>
                  </section>

                  {/* Mobile editor modal */}
                  {selectedSubagentIndex !== null && subagentItems[selectedSubagentIndex] && (
                    <div className="fixed inset-0 z-50 lg:hidden">
                      <div
                        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
                        onClick={() => setSelectedSubagentIndex(null)}
                      />
                      <div
                        className="absolute inset-x-0 bottom-0 max-h-[90vh] overflow-y-auto rounded-t-2xl bg-white p-4 shadow-2xl"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <SubAgentEditorPanel
                          index={selectedSubagentIndex}
                          item={subagentItems[selectedSubagentIndex]}
                          agentModels={agentModels}
                          imageAnalyzerModels={imageAnalyzerProviderModels
                            .map((model) => ({ id: model.id, name: model.name, providerName: model.provider.name }))}
                          imageAnalyzerModelId={imageAnalyzerBoundId}
                          refreshingModels={refreshingModels}
                          onChange={updateSubAgentItem}
                          onRefreshModels={handleRefreshAgentModels}
                          onBindImageAnalyzer={(modelId) => handleBindProvider("image_analyzer", modelId)}
                          onDelete={handleDeleteSubAgent}
                          onClose={() => setSelectedSubagentIndex(null)}
                          isMobile
                        />
                      </div>
                    </div>
                  )}

                  {/* Config preview modal */}
                  {configModalOpen && (
                    <div className="fixed inset-0 z-50">
                      <div
                        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
                        onClick={() => setConfigModalOpen(false)}
                      />
                      <div
                        className="absolute left-1/2 top-1/2 w-[calc(100%-2rem)] max-w-2xl -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-black/[0.06] bg-white p-5 shadow-2xl"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <div className="mb-4 flex items-center justify-between gap-3">
                          <div className="flex items-center gap-2">
                            <Braces className="h-4 w-4 text-[#002fa7]" />
                            <h3 className="text-[14px] font-semibold text-gray-800">SubAgent config.json</h3>
                          </div>
                          <button
                            type="button"
                            onClick={() => setConfigModalOpen(false)}
                            className="rounded-lg p-1.5 text-gray-400 hover:bg-black/[0.04] hover:text-gray-600"
                          >
                            <X className="h-4 w-4" />
                          </button>
                        </div>
                        <SpecPreview spec={subagentItemsToConfig(subagentItems)} />
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}
            {/* Worker Access */}
            {activeCategory === "worker" && <WorkerAccessKeysPanel extensions={runtimeExtensions} />}

            {/* Memory Editor */}
            {activeCategory === "memory" && (
              <div className="h-[calc(100vh-140px)]">
                <MemoryEditor />
              </div>
            )}

            {/* System Status */}
            {activeCategory === "system" && (
              <SettingsCard title="系统状态" icon={Activity} color="#002fa7">
                <CapabilitiesStatus
                  refreshIntervalMs={30000}
                  onChange={setCapabilities}
                  extensions={runtimeExtensions}
                  excludeKeys={["cli"]}
                />
              </SettingsCard>
            )}

          </div>
        </div>
      </main>
    </div>

    {providerModelPicker && modelPickerProvider && modelPickerEndpoint && (
      <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-950/35 p-4 backdrop-blur-[2px] sm:p-8" onMouseDown={() => setProviderModelPicker(null)}>
        <section
          role="dialog"
          aria-modal="true"
          aria-label={`${modelPickerProvider.name} 模型列表`}
          data-screen-label="Provider 模型选择弹窗"
          className="flex max-h-[86vh] min-h-[520px] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl shadow-slate-950/20"
          onMouseDown={(event) => event.stopPropagation()}
        >
          <header className="flex items-center justify-between gap-4 border-b border-slate-200 px-6 py-5">
            <div className="flex min-w-0 items-center gap-3">
              <ProviderLogo provider={modelPickerProvider} size="md" />
              <div className="min-w-0">
                <h2 className="truncate text-[19px] font-semibold text-slate-900">{modelPickerProvider.name} 模型</h2>
                <p className="mt-0.5 text-[11px] text-slate-500">从 Provider 返回的模型列表中搜索并登记</p>
              </div>
            </div>
            <button type="button" onClick={() => setProviderModelPicker(null)} className="rounded-lg p-2 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900" aria-label="关闭模型列表"><X className="h-5 w-5" /></button>
          </header>

          <div className="border-b border-slate-200 px-6 pt-5">
            <div className="flex gap-2">
              <label className="flex min-w-0 flex-1 items-center gap-3 rounded-xl border border-slate-300 bg-white px-4 py-3 shadow-sm transition focus-within:border-[#002fa7] focus-within:ring-2 focus-within:ring-[#002fa7]/10">
                <Search className="h-4 w-4 shrink-0 text-slate-500" />
                <input autoFocus value={providerModelSearch} onChange={(event) => setProviderModelSearch(event.target.value)} placeholder="搜索模型 ID 或名称" className="min-w-0 flex-1 bg-transparent text-[13px] text-slate-900 outline-none placeholder:text-slate-400" />
                {providerModelSearch && <button type="button" onClick={() => setProviderModelSearch("")} className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="清空搜索"><X className="h-3.5 w-3.5" /></button>}
              </label>
              <button type="button" disabled={providerBusy === `${modelPickerKey}:discover`} onClick={() => handleDiscoverProvider(modelPickerProvider, modelPickerEndpoint.id)} className="flex h-[46px] w-[46px] shrink-0 items-center justify-center rounded-xl border border-slate-300 bg-white text-slate-600 transition hover:border-slate-400 hover:bg-slate-50 disabled:opacity-50" title="刷新模型列表"><RefreshCw className={`h-4 w-4 ${providerBusy === `${modelPickerKey}:discover` ? "animate-spin" : ""}`} /></button>
            </div>
            <nav className="mt-4 flex gap-6 overflow-x-auto" aria-label="模型预分类筛选">
              {DISCOVERED_MODEL_FILTERS.map((filter) => <button type="button" key={filter.id} onClick={() => setProviderModelFilter(filter.id)} className={`shrink-0 border-b-2 px-0.5 pb-3 text-[12px] font-medium transition ${providerModelFilter === filter.id ? "border-[#002fa7] text-[#002fa7]" : "border-transparent text-slate-500 hover:text-slate-800"}`}>{filter.label}</button>)}
            </nav>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50/70 p-5 sm:p-6">
            {providerModelPickerError && <div className="mb-4 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-[12px] text-rose-700">{providerModelPickerError}</div>}
            {providerBusy === `${modelPickerKey}:discover` && !(discoveredProviderModels[modelPickerKey] || []).length ? (
              <div className="flex min-h-[260px] flex-col items-center justify-center gap-3 text-slate-500"><Loader2 className="h-6 w-6 animate-spin text-[#002fa7]" /><span className="text-[12px]">正在获取模型列表…</span></div>
            ) : groupedModelPickerModels.length === 0 ? (
              <div className="flex min-h-[260px] flex-col items-center justify-center gap-2 text-center"><Search className="h-6 w-6 text-slate-400" /><p className="text-[13px] font-medium text-slate-700">没有匹配的模型</p><p className="text-[11px] text-slate-500">请调整搜索词或切换分类</p></div>
            ) : (
              <div className="space-y-3">
                {groupedModelPickerModels.map(([family, models]) => {
                  const familyKey = `${modelPickerKey}:${family}`;
                  const expanded = providerModelSearch.trim()
                    ? true
                    : expandedDiscoveredFamilies[familyKey] ?? models.length <= 20;
                  return <div key={family} className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
                    <button type="button" onClick={() => setExpandedDiscoveredFamilies((current) => ({ ...current, [familyKey]: !expanded }))} className="flex w-full items-center gap-3 bg-slate-100/80 px-4 py-3 text-left transition hover:bg-slate-100">
                      {expanded ? <ChevronDown className="h-4 w-4 text-slate-500" /> : <ChevronUp className="h-4 w-4 text-slate-500" />}
                      <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-slate-800">{family}</span>
                      <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-medium text-slate-500 shadow-sm">{models.length}</span>
                    </button>
                    {expanded && <div>{models.map((model) => {
                      const existingModel = modelPickerProvider.models.find((item) => item.endpoint_id === modelPickerEndpoint.id && item.name === model.name);
                      const adding = providerModelAdding === model.name;
                      return <div key={model.name} className="flex min-h-[58px] items-center gap-3 border-t border-slate-100 px-4 py-3 transition hover:bg-slate-50">
                        <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-medium text-slate-800" title={model.name}>{model.name}</span>
                        {existingModel && <div className="hidden flex-wrap justify-end gap-1 sm:flex">{modelCategories(existingModel).map((category) => <span key={category} className="rounded-md bg-slate-100 px-2 py-1 text-[10px] text-slate-600">{MODEL_CATEGORY_LABELS[category]}</span>)}</div>}
                        {existingModel ? <button type="button" disabled={adding || providerModelAdding !== null} onClick={() => openModelCategoryEditor(modelPickerProvider, modelPickerEndpoint.id, model.name, existingModel)} className="rounded-lg border border-slate-200 px-2.5 py-1.5 text-[10px] font-medium text-slate-600 transition hover:border-[#002fa7]/30 hover:bg-[#002fa7]/5 hover:text-[#002fa7]">编辑分类</button> : <button type="button" disabled={adding || providerModelAdding !== null} onClick={() => openModelCategoryEditor(modelPickerProvider, modelPickerEndpoint.id, model.name)} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-200 text-slate-600 transition hover:border-[#002fa7]/40 hover:bg-[#002fa7]/5 hover:text-[#002fa7] disabled:opacity-45" title={`添加 ${model.name}`}>{adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}</button>}
                      </div>;
                    })}</div>}
                  </div>;
                })}
              </div>
            )}
          </div>

          <footer className="flex items-center justify-between gap-4 border-t border-slate-200 bg-white px-6 py-3.5">
            <p className="text-[11px] text-slate-500">共 {(discoveredProviderModels[modelPickerKey] || []).length} 个模型 · 当前显示 {modelPickerModels.length} 个</p>
            <button type="button" onClick={() => setProviderModelPicker(null)} className="rounded-lg border border-slate-300 px-4 py-2 text-[11px] font-medium text-slate-700 transition hover:bg-slate-50">完成</button>
          </footer>
        </section>
      </div>
    )}

    {pendingModelCategoryEdit && pendingModelProvider && pendingModelEndpoint && (
      <div className="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/35 p-4 backdrop-blur-[2px]" onMouseDown={() => setPendingModelCategoryEdit(null)}>
        <section role="dialog" aria-modal="true" aria-label="编辑模型分类" className="w-full max-w-lg overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl shadow-slate-950/25" onMouseDown={(event) => event.stopPropagation()}>
          <header className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
            <div className="min-w-0">
              <h3 className="text-[16px] font-semibold text-slate-900">选择模型分类</h3>
              <p className="mt-1 truncate font-mono text-[11px] text-slate-500">{pendingModelProvider.name} · {pendingModelCategoryEdit.name}</p>
            </div>
            <button type="button" onClick={() => setPendingModelCategoryEdit(null)} className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700" aria-label="关闭分类设置"><X className="h-4 w-4" /></button>
          </header>
          <div className="p-5">
            <p className="mb-3 text-[11px] leading-5 text-slate-500">已按模型名完成一次预选，请确认后保存。分类可以增删；同一个模型只能使用一种调用协议。</p>
            <div className="space-y-2">
              {editableModelCategories.map((option) => {
                const checked = selectedModelCategories.includes(option.id);
                const disabled = !checked && Boolean(pendingCategoryCapability && pendingCategoryCapability !== option.capability);
                return <button type="button" key={option.id} disabled={disabled} onClick={() => setSelectedModelCategories((current) => checked ? current.filter((category) => category !== option.id) : [...current, option.id])} className={`flex w-full items-center gap-3 rounded-xl border px-4 py-3 text-left transition ${checked ? "border-[#002fa7]/35 bg-[#002fa7]/[0.06]" : disabled ? "cursor-not-allowed border-slate-100 bg-slate-50 opacity-45" : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"}`}>
                  <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${checked ? "border-[#002fa7] bg-[#002fa7] text-white" : "border-slate-300 bg-white text-transparent"}`}><CheckCircle2 className="h-3.5 w-3.5" /></span>
                  <span className="min-w-0 flex-1"><span className="block text-[12px] font-semibold text-slate-800">{option.label}</span><span className="mt-0.5 block text-[10px] text-slate-500">{option.description}</span></span>
                </button>;
              })}
            </div>
          </div>
          <footer className="flex items-center justify-end gap-2 border-t border-slate-200 bg-slate-50/70 px-5 py-4">
            <button type="button" onClick={() => setPendingModelCategoryEdit(null)} className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-[11px] font-medium text-slate-700 transition hover:bg-slate-50">取消</button>
            <button type="button" disabled={selectedModelCategories.length === 0 || providerModelAdding !== null} onClick={() => void handleSaveModelCategories()} className="inline-flex min-w-[88px] items-center justify-center gap-1.5 rounded-lg bg-[#002fa7] px-4 py-2 text-[11px] font-semibold text-white transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-45">{providerModelAdding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}保存</button>
          </footer>
        </section>
      </div>
    )}

    {/* Toast */}
    {toast && (
      <div className={`fixed bottom-6 right-6 z-[70] flex items-center gap-2 px-4 py-2.5 rounded-xl text-[13px] font-medium shadow-lg animate-fade-in ${
        toast.type === "success"
          ? "bg-emerald-500 text-white"
          : "bg-red-500 text-white"
      }`}>
        {toast.type === "success" ? <CheckCircle2 className="w-4 h-4" /> : <XCircle className="w-4 h-4" />}
        {toast.message}
      </div>
    )}
  </div>
);
}

// ── Reusable Components ───────────────────────────────────

const SUBAGENT_EDITOR_TABS = [
  { key: "basic", label: "基本", description: "名称、模型与 Prompt" },
  { key: "input", label: "输入识别", description: "路由触发条件" },
  { key: "tools", label: "Tools / Skills", description: "工具与技能继承" },
  { key: "advanced", label: "高级策略", description: "HITL 与权限预留" },
] as const;

type SubAgentEditorTab = (typeof SUBAGENT_EDITOR_TABS)[number]["key"];

function SubAgentEditorPanel({
  index,
  item,
  agentModels,
  imageAnalyzerModels,
  imageAnalyzerModelId,
  refreshingModels,
  onChange,
  onRefreshModels,
  onBindImageAnalyzer,
  onDelete,
  onClose,
  isMobile,
}: {
  index: number;
  item: SubAgentItem;
  agentModels: string[];
  imageAnalyzerModels: Array<{ id: string; name: string; providerName: string }>;
  imageAnalyzerModelId: string;
  refreshingModels: boolean;
  onChange: (index: number, updater: (item: SubAgentItem) => SubAgentItem) => void;
  onRefreshModels: () => void;
  onBindImageAnalyzer: (modelId: string) => void | Promise<void>;
  onDelete: (index: number) => void;
  onClose: () => void;
  isMobile?: boolean;
}) {
  const [activeTab, setActiveTab] = useState<SubAgentEditorTab>("basic");
  const isImageAnalyzer = item.name === "image_analyzer" || item.route_trigger === "image_input";

  return (
    <div className={`flex h-full flex-col rounded-xl border border-black/[0.06] bg-white ${isMobile ? "p-4" : "p-5"}`}>
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h3 className="text-[14px] font-semibold text-gray-800">SubAgent #{index + 1} 配置</h3>
          <p className="mt-0.5 text-[12px] text-gray-500">{item.name || "未命名子代理"}</p>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => {
              onDelete(index);
              onClose();
            }}
            className="rounded-lg px-2.5 py-1.5 text-[11px] font-medium text-red-600 hover:bg-red-50"
          >
            删除
          </button>
          {isMobile && (
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1.5 text-gray-400 hover:bg-black/[0.04] hover:text-gray-600"
              title="关闭配置"
              aria-label="关闭 SubAgent 配置"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>

      <div className="mb-4 rounded-xl border border-black/[0.055] bg-white p-1 shadow-sm">
        <div className="grid grid-cols-4 gap-1">
          {SUBAGENT_EDITOR_TABS.map((tab) => {
            const active = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveTab(tab.key)}
                className={`flex min-w-0 flex-col items-center justify-center rounded-lg px-2 py-2 text-center transition-all ${
                  active
                    ? "bg-[#002fa7]/[0.07] text-[#002fa7] shadow-sm"
                    : "text-gray-500 hover:bg-black/[0.035] hover:text-gray-800"
                }`}
              >
                <span className="text-[12px] font-semibold">{tab.label}</span>
                <span className="mt-0.5 hidden truncate text-[9px] opacity-65 sm:block">{tab.description}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto">
        {activeTab === "basic" && (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FormField label="子代理名称 *">
                <input
                  type="text"
                  value={item.name}
                  onChange={(e) => onChange(index, (it) => ({ ...it, name: e.target.value }))}
                  className="form-input h-11"
                  placeholder="image_analyzer"
                />
              </FormField>
              <div>
                {isImageAnalyzer ? (
                  <>
                    <label className="mb-1.5 block text-[12px] font-medium text-gray-700">图片理解模型 *</label>
                    <ModelBindingSelect
                      value={imageAnalyzerModelId}
                      onChange={onBindImageAnalyzer}
                      emptyLabel="请先添加多模态模型"
                      variant="light"
                      options={imageAnalyzerModels.map((model) => ({ id: model.id, label: `${model.providerName} · ${model.name}` }))}
                    />
                    <p className="mt-1 text-[10px] text-gray-500">
                      与「模型服务 → 默认模型 → 图片理解模型」同步，运行时由 image_analyzer 绑定读取。请选择支持图片输入的模型。
                    </p>
                  </>
                ) : (
                  <>
                    <div className="mb-1.5 flex items-center justify-between">
                      <label className="text-[12px] font-medium text-gray-700">模型 *</label>
                      <button
                        type="button"
                        onClick={onRefreshModels}
                        disabled={refreshingModels}
                        className="flex items-center gap-1 text-[10px] text-[#002fa7] hover:text-[#001f7a] disabled:opacity-50"
                      >
                        <RefreshCw className={`h-3 w-3 ${refreshingModels ? "animate-spin" : ""}`} />
                        刷新模型
                      </button>
                    </div>
                    <select
                      value={item.model}
                      onChange={(e) => onChange(index, (it) => ({ ...it, model: e.target.value }))}
                      className="form-select"
                      disabled={agentModels.length === 0}
                    >
                      {agentModels.length === 0 ? (
                        <option value="">暂无可用模型</option>
                      ) : (
                        agentModels.map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))
                      )}
                    </select>
                  </>
                )}
              </div>
            </div>
            <FormField label="描述 *">
              <input
                type="text"
                value={item.description}
                onChange={(e) => onChange(index, (it) => ({ ...it, description: e.target.value }))}
                className="form-input"
                placeholder="Analyze image inputs..."
              />
            </FormField>
            <FormField label="System Prompt *">
              <textarea
                value={item.system_prompt}
                onChange={(e) => onChange(index, (it) => ({ ...it, system_prompt: e.target.value }))}
                rows={5}
                className="form-input resize-none overflow-y-auto leading-relaxed"
              />
            </FormField>
          </div>
        )}

        {activeTab === "input" && (
          <div className="space-y-3">
            <FormField label="路由提示">
              <input
                type="text"
                value={item.route_trigger}
                onChange={(e) => onChange(index, (it) => ({ ...it, route_trigger: e.target.value }))}
                className="form-input"
                placeholder="image_input"
              />
              <p className="mt-1 text-[10px] text-gray-400">
                给主 LLM 看的委派提示，不是自动路由开关；实际调用仍由主 Agent 决定是否使用 task 子代理。
              </p>
            </FormField>
            <div className="rounded-xl border border-emerald-500/10 bg-emerald-50 px-3.5 py-3">
              <p className="text-[12px] font-medium text-emerald-800">已支持的输入</p>
              <p className="mt-1 text-[11px] leading-relaxed text-emerald-700">
                Agent 模式输入框可上传图片；用户消息中的本地图片路径也会被后端识别，并转换为多模态模型可读的 image_url。
              </p>
            </div>
          </div>
        )}

        {activeTab === "tools" && (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FormField label="Tools">
                <select
                  value={item.tools.mode}
                  onChange={(e) =>
                    onChange(index, (it) => ({
                      ...it,
                      tools: { mode: e.target.value === "none" ? "none" : "inherit" },
                    }))
                  }
                  className="form-select"
                >
                  <option value="inherit">继承主 Agent 工具</option>
                  <option value="none">不授予工具</option>
                </select>
              </FormField>
              <FormField label="Skills">
                <select
                  value={item.skills.mode}
                  onChange={(e) => {
                    const value = e.target.value;
                    const mode = value === "custom" || value === "none" ? value : "inherit";
                    onChange(index, (it) => ({ ...it, skills: { ...it.skills, mode } }));
                  }}
                  className="form-select"
                >
                  <option value="inherit">继承主 Agent skills</option>
                  <option value="custom">自定义 skill 路径</option>
                  <option value="none">不注入 skills</option>
                </select>
              </FormField>
            </div>
            {item.skills.mode === "custom" && (
              <FormField label="Skill paths（每行一个，例如 /skills/）">
                <textarea
                  value={item.skills.paths.join("\n")}
                  onChange={(e) =>
                    onChange(index, (it) => ({
                      ...it,
                      skills: {
                        ...it.skills,
                        paths: e.target.value
                          .split(/\r?\n/)
                          .map((path) => path.trim())
                          .filter(Boolean),
                      },
                    }))
                  }
                  rows={5}
                  className="form-input resize-none leading-relaxed"
                  placeholder="/skills/"
                />
              </FormField>
            )}
          </div>
        )}

        {activeTab === "advanced" && (
          <div className="space-y-3">
            <div className="rounded-xl border border-black/[0.06] bg-slate-50 px-3.5 py-4 text-center">
              <p className="text-[12px] font-medium text-gray-700">高级策略预留</p>
              <p className="mt-1 text-[11px] text-gray-500">
                后续可在此配置 human-in-the-loop、权限规则、中断条件等 DeepAgents SubAgent 高级选项。
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function SpecPreview({ spec }: { spec: unknown }) {
  return (
    <div className="rounded-xl border border-black/[0.06] bg-gray-950 p-4 text-gray-100 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Braces className="h-4 w-4 text-[#8fb1ff]" />
          <h3 className="text-[12px] font-semibold">config.json spec</h3>
        </div>
        <span className="rounded-full bg-white/10 px-2 py-0.5 text-[10px] text-gray-300">保存后生效</span>
      </div>
      <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-gray-200">
        {JSON.stringify(spec, null, 2)}
      </pre>
    </div>
  );
}

function SwitchButton({
  checked,
  onChange,
  ariaLabel,
  disabled = false,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  ariaLabel: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      aria-disabled={disabled}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 shrink-0 rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#002fa7]/40 disabled:cursor-not-allowed disabled:opacity-50 ${
        disabled ? "cursor-not-allowed" : "cursor-pointer"
      } ${
        checked ? "bg-[#002fa7]" : "bg-gray-300"
      }`}
    >
      <span
        className={`pointer-events-none mt-0.5 inline-block h-5 w-5 rounded-full bg-white shadow-sm transition-transform ${
          checked ? "translate-x-[22px]" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

function ToggleRow({
  label,
  description,
  checked,
  onChange,
  disabled = false,
}: {
  label: string;
  description?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className={`flex items-center justify-between gap-3 rounded-lg border border-black/[0.06] bg-white/60 px-3 py-2 ${
      disabled ? "opacity-50" : ""
    }`}>
      <span className="min-w-0">
        <span className="block text-[11px] font-medium text-gray-600">{label}</span>
        {description ? <span className="mt-0.5 block text-[10px] leading-4 text-gray-400">{description}</span> : null}
      </span>
      <SwitchButton checked={checked} onChange={onChange} ariaLabel={label} disabled={disabled} />
    </div>
  );
}

function SettingsWorkspaceHeader({
  category,
  description,
  onSave,
  saving,
  showSave,
}: {
  category: SettingsCategory;
  description: string;
  onSave: () => void;
  saving: boolean;
  showSave: boolean;
}) {
  const definition = SETTINGS_CATEGORIES.find((item) => item.key === category);
  const Icon = definition?.icon || Activity;

  return (
    <header className="flex flex-col gap-4 border-b border-black/[0.06] pb-6 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex min-w-0 items-start gap-3.5">
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.08] text-[#002fa7]">
          <Icon className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <h1 className="text-[22px] font-semibold tracking-tight text-gray-900">
            {definition?.label || "设置"}
          </h1>
          <p className="mt-1 max-w-2xl text-[12px] leading-5 text-gray-500">{description}</p>
        </div>
      </div>
      {showSave ? (
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          className="inline-flex h-10 shrink-0 items-center justify-center gap-2 self-start rounded-xl bg-[#002fa7] px-4 text-[12px] font-medium text-white shadow-sm transition hover:bg-[#001f7a] disabled:cursor-wait disabled:opacity-50"
        >
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          保存设置
        </button>
      ) : null}
    </header>
  );
}

function SettingsCard({
  title,
  icon: Icon,
  color,
  children,
}: {
  title: string;
  icon: React.ElementType;
  color: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white rounded-2xl border border-black/[0.055] p-5 shadow-sm">
      <div className="flex items-center gap-2 mb-4">
        <Icon className="w-4 h-4" style={{ color }} />
        <h2 className="text-[14px] font-semibold text-gray-800">{title}</h2>
      </div>
      <div className="space-y-4">
        {children}
      </div>
    </div>
  );
}

function FormField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-[11px] font-medium text-gray-500 mb-1.5">{label}</label>
      {children}
    </div>
  );
}

function DatabaseQaParameterField({
  label,
  description,
  unit,
  value,
  onChange,
  disabled = false,
}: {
  label: string;
  description: string;
  unit: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className={disabled ? "opacity-50" : ""}>
      <label className="mb-1.5 block text-[11px] font-medium text-gray-600">{label}</label>
      <div className="relative">
        <input
          type="number"
          min="1"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          disabled={disabled}
          className="form-input settings-number-input pr-16 disabled:cursor-not-allowed disabled:bg-gray-50"
          inputMode="numeric"
        />
        <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-[10px] font-medium text-gray-400">
          {unit}
        </span>
      </div>
      <p className="mt-1.5 text-[10px] leading-4 text-gray-400">{description}</p>
    </div>
  );
}
