"use client";

import { useState, useEffect, useCallback } from "react";
import {
  Bot,
  Database,
  FileText,
  Sliders,
  Brain,
  Save,
  Loader2,
  CheckCircle2,
  XCircle,
  Eye,
  EyeOff,
  Zap,
  ArrowLeft,
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
} from "lucide-react";
import {
  getSettings,
  updateSettings,
  resetKnowledgeVectorCollections,
  testConnection,
  testDatabaseConnection,
  getCapabilities,
  probeHarnessDocker,
  type SystemSettings,
  type Capabilities,
  type SubAgentItem,
} from "@/lib/settingsApi";
import { useApp } from "@/lib/store";
import {
  getProjectContext,
  updateProjectContext,
  type ProjectContextDocument,
} from "@/lib/api";
import MemoryEditor from "@/components/settings/MemoryEditor";
import CapabilitiesStatus from "@/components/settings/CapabilitiesStatus";
import Navbar from "@/components/layout/Navbar";
import Link from "next/link";

type SettingsCategory = "ai" | "project" | "databaseQa" | "rag" | "knowledge" | "memory" | "harness" | "advanced" | "system";
type SubAgentConfigMap = Record<string, Omit<SubAgentItem, "name">>;

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
  { id: "completion", label: "Goal 与验收", description: "Run Rubric 与显式 Goal", icon: Target },
  { id: "sandbox", label: "终端与沙箱", description: "Docker 后端与受控降级", icon: Box },
  { id: "runtime", label: "运行保护", description: "运行保护与权限策略", icon: ShieldCheck },
];

const CATEGORIES: { key: SettingsCategory; label: string; icon: React.ElementType; color: string }[] = [
  { key: "ai", label: "AI 网关", icon: Network, color: "#002fa7" },
  { key: "project", label: "项目上下文", icon: FileText, color: "#002fa7" },
  { key: "databaseQa", label: "智能问数设置", icon: Database, color: "#002fa7" },
  { key: "rag", label: "RAG 设置", icon: Database, color: "#002fa7" },
  { key: "knowledge", label: "知识库", icon: FolderOpen, color: "#002fa7" },
  { key: "memory", label: "记忆管理", icon: Brain, color: "#002fa7" },
  { key: "harness", label: "Harness 配置", icon: Bot, color: "#002fa7" },
  { key: "advanced", label: "高级设置", icon: Sliders, color: "#6b7280" },
  { key: "system", label: "系统状态", icon: Activity, color: "#002fa7" },
];

const LLM_PROVIDERS = [
  { value: "deepseek", label: "DeepSeek", baseUrl: "https://api.deepseek.com" },
  { value: "qwen", label: "Qwen / DashScope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
];

const EMBEDDING_PROVIDERS = [
  { value: "qwen", label: "Qwen / DashScope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
];

const SETTINGS_CATEGORY_KEY = "settings:activeCategory";
const MANAGED_DOCKER_IMAGE = "puddingclaw/sandbox:python3.12-node22-v2";
const LEGACY_MANAGED_DOCKER_IMAGES = new Set([
  MANAGED_DOCKER_IMAGE,
  "puddingclaw/sandbox:python3.12-node22-v1",
  "python:3.12-slim",
]);
const DOCKER_CPU_OPTIONS = ["2", "4", "8", "16"];
const DOCKER_MEMORY_OPTIONS = [
  { value: "2048", label: "2 GB" },
  { value: "4096", label: "4 GB" },
  { value: "8192", label: "8 GB" },
  { value: "16384", label: "16 GB" },
];
const DEFAULT_IMAGE_ANALYZER_PROMPT =
  "You are an image analysis specialist. When given an image, describe its contents in detail and answer any questions about it. Return your findings as concise, structured text.";

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

export default function SettingsPage() {
  const { sidebarOpen, toggleSidebar, thinkingMode, setThinkingMode, currentProjectId, projects } = useApp();
  const [mounted, setMounted] = useState(false);
  const [selectedSubagentIndex, setSelectedSubagentIndex] = useState<number | null>(null);
  const [configModalOpen, setConfigModalOpen] = useState(false);
  useEffect(() => {
    setMounted(true);
    const params = new URLSearchParams(window.location.search);
    const categoryParam = params.get("category");
    if (CATEGORIES.some((item) => item.key === categoryParam)) {
      setCategory(categoryParam as SettingsCategory);
    }
  }, []);
  const [category, setCategory] = useState<SettingsCategory>(() => {
    if (typeof window === "undefined") return "ai";
    const saved = localStorage.getItem(SETTINGS_CATEGORY_KEY);
    const valid = CATEGORIES.some((c) => c.key === saved);
    return (valid ? (saved as SettingsCategory) : "ai");
  });
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const currentProject = projects.find((project) => project.project_id === currentProjectId) || null;

  // AI Gateway form state
  const [gatewayBaseUrl, setGatewayBaseUrl] = useState("");
  const [gatewayHealthPath, setGatewayHealthPath] = useState("/health");
  const [gatewayFallback, setGatewayFallback] = useState(true);
  const [gatewayEnvironmentOverride, setGatewayEnvironmentOverride] = useState(false);
  const [gatewayTesting, setGatewayTesting] = useState(false);
  const [gatewayTestResult, setGatewayTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [gatewayModels, setGatewayModels] = useState<string[]>([]);
  const [gatewayModel, setGatewayModel] = useState("deepseek-v4-flash");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);

  useEffect(() => {
    localStorage.setItem(SETTINGS_CATEGORY_KEY, category);
  }, [category]);

  // LLM form state
  const [llmProvider, setLlmProvider] = useState("deepseek");
  const [llmModel, setLlmModel] = useState("deepseek-chat");
  const [llmBaseUrl, setLlmBaseUrl] = useState("https://api.deepseek.com");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [llmApiKeyMasked, setLlmApiKeyMasked] = useState("");
  const [showLlmKey, setShowLlmKey] = useState(false);
  const [temperature, setTemperature] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState(4096);
  const [llmTesting, setLlmTesting] = useState(false);
  const [llmTestResult, setLlmTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  // Embedding form state
  const [embProvider, setEmbProvider] = useState("qwen");
  const [embModel, setEmbModel] = useState("text-embedding-v3");
  const [embDimension, setEmbDimension] = useState("1024");
  const [embBatchSize, setEmbBatchSize] = useState("20");
  const [embBaseUrl, setEmbBaseUrl] = useState("https://dashscope.aliyuncs.com/compatible-mode/v1");
  const [embApiKey, setEmbApiKey] = useState("");
  const [embApiKeyMasked, setEmbApiKeyMasked] = useState("");
  const [showEmbKey, setShowEmbKey] = useState(false);
  const [embTesting, setEmbTesting] = useState(false);
  const [embTestResult, setEmbTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  // RAG form state
  const [ragTopK, setRagTopK] = useState(3);
  const [ragThreshold, setRagThreshold] = useState(0.7);
  const [ragTextVectorWeight, setRagTextVectorWeight] = useState(0.45);
  const [ragImageVectorWeight, setRagImageVectorWeight] = useState(0.35);
  const [ragHybridCandidateTopK, setRagHybridCandidateTopK] = useState(10);
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
  const [dbQaQueryTimeoutSeconds, setDbQaQueryTimeoutSeconds] = useState("30");
  const [dbQaResultStoreEnabled, setDbQaResultStoreEnabled] = useState(true);
  const [dbQaResultStoreTtlHours, setDbQaResultStoreTtlHours] = useState("168");
  const [dbQaDefaultPageSize, setDbQaDefaultPageSize] = useState("100");
  const [dbQaMaxPageSize, setDbQaMaxPageSize] = useState("500");
  const [dbQaExportEnabled, setDbQaExportEnabled] = useState(false);
  const [dbQaProfileEnabled, setDbQaProfileEnabled] = useState(true);

  // Knowledge base
  const [databaseMode, setDatabaseMode] = useState<"bundled" | "external">("bundled");
  const [databaseHost, setDatabaseHost] = useState("127.0.0.1");
  const [databasePort, setDatabasePort] = useState("5432");
  const [databaseName, setDatabaseName] = useState("puddingclaw");
  const [databaseUsername, setDatabaseUsername] = useState("pet");
  const [databasePassword, setDatabasePassword] = useState("");
  const [databaseConfiguredBy, setDatabaseConfiguredBy] = useState("default");
  const [databaseEnvOverride, setDatabaseEnvOverride] = useState(false);
  const [databaseTesting, setDatabaseTesting] = useState(false);
  const [databaseTestResult, setDatabaseTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [knowledgeRootDir, setKnowledgeRootDir] = useState("");
  const [knowledgeConfiguredBy, setKnowledgeConfiguredBy] = useState("default");
  const [knowledgeEnvOverride, setKnowledgeEnvOverride] = useState(false);
  const [mmModel, setMmModel] = useState("qwen2.5-vl-embedding");
  const [mmDimension, setMmDimension] = useState("1024");
  const [mmConcurrency, setMmConcurrency] = useState("10");
  const [mmApiKey, setMmApiKey] = useState("");
  const [mmApiKeyMasked, setMmApiKeyMasked] = useState("");
  const [showMmKey, setShowMmKey] = useState(false);
  const [kbIndexEnabled, setKbIndexEnabled] = useState(true);
  const [kbVectorStore, setKbVectorStore] = useState("milvus");
  const [kbMilvusUri, setKbMilvusUri] = useState("http://localhost:19530");
  const [kbTextCollection, setKbTextCollection] = useState("puddingclaw_knowledge_text");
  const [kbImageCollection, setKbImageCollection] = useState("puddingclaw_knowledge_image");

  // Compression
  const [compRatio, setCompRatio] = useState(0.5);

  // Harness context engineering (DeepAgents only)
  const [contextSummaryTriggerTokens, setContextSummaryTriggerTokens] = useState("200000");
  const [toolContextEnabled, setToolContextEnabled] = useState(true);
  const [singleToolTriggerTokens, setSingleToolTriggerTokens] = useState("8000");
  const [backgroundMinResultTokens, setBackgroundMinResultTokens] = useState("1000");
  const [keepRecentToolResults, setKeepRecentToolResults] = useState("12");

  // Harness runtime policy
  const [modelCallLimitEnabled, setModelCallLimitEnabled] = useState(true);
  const [modelCallRunLimit, setModelCallRunLimit] = useState("50");
  const [modelCallThreadLimit, setModelCallThreadLimit] = useState("");
  const [modelCallExitBehavior, setModelCallExitBehavior] = useState<"end" | "error">("end");
  const [rubricEnabled, setRubricEnabled] = useState(true);
  const [rubricMaxIterations, setRubricMaxIterations] = useState("2");
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
  const [dockerEnabled, setDockerEnabled] = useState(false);
  const [dockerOnUnavailable, setDockerOnUnavailable] = useState<"fallback" | "deny">("fallback");
  const [dockerConnection, setDockerConnection] = useState("");
  const [dockerContext, setDockerContext] = useState("");
  const [dockerUseCustomImage, setDockerUseCustomImage] = useState(false);
  const [dockerImage, setDockerImage] = useState("");
  const [dockerCpuLimit, setDockerCpuLimit] = useState("2");
  const [dockerMemoryLimitMb, setDockerMemoryLimitMb] = useState("2048");
  const [dockerPidsLimit, setDockerPidsLimit] = useState("256");
  const [dockerNetworkEnabled, setDockerNetworkEnabled] = useState(false);
  const [dockerDependencySetupEnabled, setDockerDependencySetupEnabled] = useState(false);
  const [dockerProbeStatus, setDockerProbeStatus] = useState<"idle" | "loading" | "ok" | "error">("idle");
  const [dockerProbeDetail, setDockerProbeDetail] = useState("");

  // Harness left-right anchor layout
  const [harnessFilter, setHarnessFilter] = useState("");
  const [activeHarnessSection, setActiveHarnessSection] = useState("subagent");

  // SubAgent / Harness
  const [subagentItems, setSubagentItems] = useState<SubAgentItem[]>([]);
  const [refreshingModels, setRefreshingModels] = useState(false);

  // Project context
  const [projectContextDoc, setProjectContextDoc] = useState<ProjectContextDocument | null>(null);
  const [projectContextContent, setProjectContextContent] = useState("");
  const [projectContextLoading, setProjectContextLoading] = useState(false);
  const [projectContextSaving, setProjectContextSaving] = useState(false);

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
    Promise.all([getSettings(), getCapabilities().catch(() => null)])
      .then(([s, caps]) => {
        setSettings(s);
        setCapabilities(caps);
        setGatewayBaseUrl(s.ai_gateway.base_url);
        setGatewayHealthPath(s.ai_gateway.health_path);
        setGatewayFallback(s.ai_gateway.fallback_to_direct);
        setGatewayEnvironmentOverride(s.ai_gateway.environment_override);
        setGatewayModels(s.ai_gateway.routed_models || []);
        setGatewayModel(s.gateway_llm?.model || s.fallback_llm.model);
        // Populate LLM fields
        setLlmProvider(s.fallback_llm.provider);
        setLlmModel(s.fallback_llm.model);
        setLlmBaseUrl(s.fallback_llm.base_url);
        setLlmApiKeyMasked(s.fallback_llm.api_key_masked);
        setTemperature(s.fallback_llm.temperature);
        setMaxTokens(s.fallback_llm.max_tokens);
        // Populate Embedding fields
        const validEmbProvider = EMBEDDING_PROVIDERS.some((p) => p.value === s.fallback_embedding.provider)
          ? s.fallback_embedding.provider
          : "qwen";
        setEmbProvider(validEmbProvider);
        setEmbModel(s.fallback_embedding.model);
        setEmbDimension(String(s.fallback_embedding.dimension || 1024));
        setEmbBatchSize(String(s.fallback_embedding.batch_size || 20));
        setEmbBaseUrl(
          EMBEDDING_PROVIDERS.find((p) => p.value === validEmbProvider)?.baseUrl ?? s.fallback_embedding.base_url
        );
        setEmbApiKeyMasked(s.fallback_embedding.api_key_masked);
        // Populate RAG fields
        setRagTopK(s.rag.top_k);
        setRagThreshold(s.rag.similarity_threshold);
        const textWeight = s.rag.hybrid?.text_vector_weight ?? 0.45;
        const keywordWeight = s.rag.hybrid?.bm25_weight ?? 0.2;
        const textMixTotal = textWeight + keywordWeight;
        setRagTextVectorWeight(textMixTotal > 0 ? textWeight / textMixTotal : 0.7);
        setRagImageVectorWeight(s.rag.hybrid?.image_vector_weight ?? 0.35);
        setRagHybridCandidateTopK(s.rag.hybrid?.candidate_top_k ?? 10);
        setRagRerankEnabled(s.rag.rerank?.enabled ?? true);
        setRagRerankCandidateTopK(s.rag.rerank?.candidate_top_k ?? 50);
        const databaseQa = s.analytics?.database_qa;
        setDbQaFullRowsTokenBudget(String(databaseQa?.full_rows_token_budget ?? 10000));
        setDbQaPreviewRowsTokenBudget(String(databaseQa?.preview_rows_token_budget ?? 3000));
        setDbQaProfileTokenBudget(String(databaseQa?.profile_token_budget ?? 3000));
        setDbQaFullRowsHardRowCap(String(databaseQa?.full_rows_hard_row_cap ?? 200));
        setDbQaFullRowsHardColumnCap(String(databaseQa?.full_rows_hard_column_cap ?? 20));
        setDbQaMaxCellCharsForLlm(String(databaseQa?.max_cell_chars_for_llm ?? 500));
        setDbQaQueryTimeoutSeconds(String(Math.max(1, Math.round((databaseQa?.query_timeout_ms ?? 30000) / 1000))));
        setDbQaResultStoreEnabled(databaseQa?.result_store_enabled ?? true);
        setDbQaResultStoreTtlHours(String(databaseQa?.result_store_ttl_hours ?? 168));
        setDbQaDefaultPageSize(String(databaseQa?.default_page_size ?? 100));
        setDbQaMaxPageSize(String(databaseQa?.max_page_size ?? 500));
        setDbQaExportEnabled(databaseQa?.export_enabled ?? false);
        setDbQaProfileEnabled(databaseQa?.profile_enabled ?? true);
        // Knowledge base
        setDatabaseMode(s.database?.mode === "external" ? "external" : "bundled");
        setDatabaseHost(s.database?.host || "127.0.0.1");
        setDatabasePort(String(s.database?.port || 5432));
        setDatabaseName(s.database?.database || "puddingclaw");
        setDatabaseUsername(s.database?.username || "puddingclaw");
        setDatabasePassword(s.database?.password || "puddingclaw");
        setDatabaseConfiguredBy(s.database?.configured_by || "default");
        setDatabaseEnvOverride(Boolean(s.database?.environment_override));
        setKnowledgeRootDir(s.knowledge?.root_dir || "");
        setKnowledgeConfiguredBy(s.knowledge?.configured_by || "default");
        setKnowledgeEnvOverride(Boolean(s.knowledge?.environment_override));
        setMmModel(s.multimodal_embedding?.model || "qwen2.5-vl-embedding");
        setMmDimension(String(s.multimodal_embedding?.dimension || 1024));
        setMmConcurrency(String(s.multimodal_embedding?.batch_size || 10));
        setMmApiKeyMasked(s.multimodal_embedding?.api_key_masked || "");
        setKbIndexEnabled(s.knowledge?.multimodal_index?.enabled ?? true);
        setKbVectorStore(s.knowledge?.multimodal_index?.vector_store || "milvus");
        setKbMilvusUri(s.knowledge?.multimodal_index?.milvus_uri || "http://localhost:19530");
        setKbTextCollection(s.knowledge?.multimodal_index?.text_collection || "puddingclaw_knowledge_text");
        setKbImageCollection(s.knowledge?.multimodal_index?.image_collection || "puddingclaw_knowledge_image");
        // Compression
        setCompRatio(s.compression.ratio);
        setContextSummaryTriggerTokens(
          String(s.compression.deepagents?.summarization?.trigger_tokens ?? 200000)
        );
        setToolContextEnabled(s.compression.deepagents?.tool_context?.enabled ?? true);
        setSingleToolTriggerTokens(
          String(s.compression.deepagents?.tool_context?.single_tool_trigger_tokens ?? 8000)
        );
        setBackgroundMinResultTokens(
          String(s.compression.deepagents?.tool_context?.background_min_result_tokens ?? 1000)
        );
        setKeepRecentToolResults(
          String(s.compression.deepagents?.tool_context?.keep_recent_tool_results ?? 12)
        );
        // Harness runtime policy
        const modelLimit = s.harness?.model_call_limit;
        setModelCallLimitEnabled(modelLimit?.enabled ?? true);
        setModelCallRunLimit(modelLimit?.run_limit ? String(modelLimit.run_limit) : "50");
        setModelCallThreadLimit(modelLimit?.thread_limit ? String(modelLimit.thread_limit) : "");
        setModelCallExitBehavior(modelLimit?.exit_behavior === "error" ? "error" : "end");
        const rubric = s.harness?.completion?.rubric;
        setRubricEnabled(rubric?.enabled ?? true);
        setRubricMaxIterations(String(rubric?.max_iterations ?? 2));
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
        setDockerEnabled(terminal?.docker_enabled ?? false);
        setDockerOnUnavailable(terminal?.on_unavailable === "deny" ? "deny" : "fallback");
        setDockerConnection(terminal?.docker?.connection || "");
        setDockerContext(terminal?.docker?.context || "");
        const configuredDockerImage = terminal?.docker?.image || MANAGED_DOCKER_IMAGE;
        const usesCustomDockerImage = !LEGACY_MANAGED_DOCKER_IMAGES.has(configuredDockerImage);
        setDockerUseCustomImage(usesCustomDockerImage);
        setDockerImage(usesCustomDockerImage ? configuredDockerImage : "");
        const configuredCpuLimit = terminal?.docker?.cpu_limit || "2";
        const configuredMemoryLimit = String(terminal?.docker?.memory_limit_mb ?? 2048);
        setDockerCpuLimit(DOCKER_CPU_OPTIONS.includes(configuredCpuLimit) ? configuredCpuLimit : "2");
        setDockerMemoryLimitMb(
          DOCKER_MEMORY_OPTIONS.some((option) => option.value === configuredMemoryLimit)
            ? configuredMemoryLimit
            : "2048"
        );
        setDockerPidsLimit(String(terminal?.docker?.pids_limit ?? 256));
        setDockerNetworkEnabled(terminal?.docker?.network_enabled ?? false);
        setDockerDependencySetupEnabled(
          terminal?.docker?.dependency_setup_enabled === true
          && terminal?.docker?.dependency_setup_opt_in_version === 1
        );
        // SubAgent
        const items = s.subagents?.items || s.subagent?.items;
        if (Array.isArray(items) && items.length > 0) {
          setSubagentItems(items);
          setSelectedSubagentIndex(0);
        } else {
          setSubagentItems([makeDefaultSubAgentItem(s.ai_gateway.routed_models || [])]);
          setSelectedSubagentIndex(0);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const showToast = useCallback((type: "success" | "error", message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 3000);
  }, []);

  useEffect(() => {
    if (!currentProjectId) {
      setProjectContextDoc(null);
      setProjectContextContent("");
      return;
    }

    let cancelled = false;
    setProjectContextLoading(true);
    getProjectContext(currentProjectId)
      .then((doc) => {
        if (cancelled) return;
        setProjectContextDoc(doc);
        setProjectContextContent(doc.content);
      })
      .catch((err) => {
        if (cancelled) return;
        setProjectContextDoc(null);
        setProjectContextContent("");
        showToast("error", err instanceof Error ? err.message : "加载项目上下文失败");
      })
      .finally(() => {
        if (!cancelled) setProjectContextLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [currentProjectId, showToast]);

  const handleSaveProjectContext = useCallback(async () => {
    if (!currentProjectId) return;
    setProjectContextSaving(true);
    try {
      const doc = await updateProjectContext(currentProjectId, projectContextContent);
      setProjectContextDoc(doc);
      setProjectContextContent(doc.content);
      showToast("success", "项目上下文已保存");
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "项目上下文保存失败");
    } finally {
      setProjectContextSaving(false);
    }
  }, [currentProjectId, projectContextContent, showToast]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const summaryTrigger = positiveIntOrNull(contextSummaryTriggerTokens) ?? 200000;
      const singleToolTrigger = positiveIntOrNull(singleToolTriggerTokens) ?? 8000;
      const backgroundMinimum = positiveIntOrNull(backgroundMinResultTokens) ?? 1000;
      const keepRecentResults = positiveIntOrNull(keepRecentToolResults) ?? 12;
      if (summaryTrigger < 10000 || summaryTrigger > 1000000) {
        throw new Error("全局摘要阈值必须在 10,000 到 1,000,000 tokens 之间");
      }
      if (singleToolTrigger < 1000 || singleToolTrigger > 100000) {
        throw new Error("执行中单条工具阈值必须在 1,000 到 100,000 tokens 之间");
      }
      if (backgroundMinimum < 100 || backgroundMinimum >= singleToolTrigger) {
        throw new Error("静默压缩单条下限必须至少为 100 且小于执行中单条工具阈值");
      }
      if (keepRecentResults < 1 || keepRecentResults > 100) {
        throw new Error("保留最近工具结果必须在 1 到 100 条之间");
      }
      if (dockerUseCustomImage && !dockerImage.trim()) {
        throw new Error("启用自定义 Docker 镜像后必须填写镜像引用");
      }
      await updateSettings({
        ai_gateway: {
          base_url: gatewayBaseUrl,
          health_path: gatewayHealthPath,
          fallback_to_direct: gatewayFallback,
        },
        gateway_llm: {
          model: gatewayModel,
        },
        fallback_llm: {
          provider: llmProvider,
          model: llmModel,
          base_url: llmBaseUrl,
          ...(llmApiKey ? { api_key: llmApiKey } : {}),
          temperature,
          max_tokens: maxTokens,
        },
        fallback_embedding: {
          provider: embProvider,
          model: embModel,
          dimension: Number.parseInt(embDimension, 10) || 1024,
          batch_size: Number.parseInt(embBatchSize, 10) || 20,
          base_url: embBaseUrl,
          ...(embApiKey ? { api_key: embApiKey } : {}),
        },
        rag: {
          enabled: true,
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
            base_url: "",
          },
        },
        analytics: {
          database_qa: {
            full_rows_token_budget: positiveIntOrNull(dbQaFullRowsTokenBudget) ?? 10000,
            preview_rows_token_budget: positiveIntOrNull(dbQaPreviewRowsTokenBudget) ?? 3000,
            profile_token_budget: positiveIntOrNull(dbQaProfileTokenBudget) ?? 3000,
            full_rows_hard_row_cap: positiveIntOrNull(dbQaFullRowsHardRowCap) ?? 200,
            full_rows_hard_column_cap: positiveIntOrNull(dbQaFullRowsHardColumnCap) ?? 20,
            max_cell_chars_for_llm: positiveIntOrNull(dbQaMaxCellCharsForLlm) ?? 500,
            query_timeout_ms: (positiveIntOrNull(dbQaQueryTimeoutSeconds) ?? 30) * 1000,
            result_store_enabled: dbQaResultStoreEnabled,
            result_store_ttl_hours: positiveIntOrNull(dbQaResultStoreTtlHours) ?? 168,
            default_page_size: positiveIntOrNull(dbQaDefaultPageSize) ?? 100,
            max_page_size: positiveIntOrNull(dbQaMaxPageSize) ?? 500,
            export_enabled: dbQaExportEnabled,
            profile_enabled: dbQaProfileEnabled,
          },
        },
        database: {
          mode: databaseMode,
          host: databaseHost || "127.0.0.1",
          port: positiveIntOrNull(databasePort) ?? 5432,
          database: databaseName || "puddingclaw",
          username: databaseUsername || "puddingclaw",
          password: databasePassword,
          url: "",
        },
        multimodal_embedding: {
          provider: "dashscope",
          model: mmModel,
          dimension: Number.parseInt(mmDimension, 10) || 1024,
          batch_size: Number.parseInt(mmConcurrency, 10) || 10,
          base_url: "",
          prefer_gateway: false,
          ...(mmApiKey ? { api_key: mmApiKey } : {}),
        },
        knowledge: {
          root_dir: knowledgeRootDir,
          multimodal_index: {
            enabled: kbIndexEnabled,
            vector_store: kbVectorStore,
            milvus_uri: kbMilvusUri,
            text_collection: kbTextCollection,
            image_collection: kbImageCollection,
          },
        },
        compression: {
          ratio: compRatio,
          deepagents: {
            summarization: {
              trigger_tokens: summaryTrigger,
            },
            tool_context: {
              enabled: toolContextEnabled,
              single_tool_trigger_tokens: singleToolTrigger,
              background_min_result_tokens: backgroundMinimum,
              keep_recent_tool_results: keepRecentResults,
            },
          },
        },
        harness: {
          model_call_limit: {
            enabled: modelCallLimitEnabled,
            run_limit: positiveIntOrNull(modelCallRunLimit) ?? 50,
            thread_limit: positiveIntOrNull(modelCallThreadLimit),
            exit_behavior: modelCallExitBehavior,
          },
          completion: {
            rubric: {
              enabled: rubricEnabled,
              max_iterations: positiveIntOrNull(rubricMaxIterations) ?? 2,
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
            docker_enabled: dockerEnabled,
            on_unavailable: dockerOnUnavailable,
            default_timeout_seconds: 120,
            docker: {
              connection: dockerConnection,
              context: dockerContext,
              image: dockerUseCustomImage
                ? dockerImage.trim()
                : MANAGED_DOCKER_IMAGE,
              cpu_limit: dockerCpuLimit,
              memory_limit_mb: positiveIntOrNull(dockerMemoryLimitMb) ?? 2048,
              pids_limit: positiveIntOrNull(dockerPidsLimit) ?? 256,
              network_enabled: dockerNetworkEnabled,
              dependency_setup_enabled: dockerDependencySetupEnabled,
              dependency_setup_opt_in_version: 1,
              lifecycle: "project",
              idle_stop_minutes: 30,
            },
          },
        },
        subagents: subagentItemsToConfig(subagentItems),
      });
      showToast("success", "设置已保存，将从下一次 Agent 运行生效");
      // Clear raw keys after save
      setLlmApiKey("");
      setEmbApiKey("");
      setMmApiKey("");
      // Reload to get fresh masked keys
      const fresh = await getSettings();
      setLlmApiKeyMasked(fresh.fallback_llm.api_key_masked);
      setEmbApiKeyMasked(fresh.fallback_embedding.api_key_masked);
      setMmApiKeyMasked(fresh.multimodal_embedding.api_key_masked);
      setDatabaseConfiguredBy(fresh.database?.configured_by || "default");
      setDatabaseEnvOverride(Boolean(fresh.database?.environment_override));
      setKnowledgeConfiguredBy(fresh.knowledge?.configured_by || "default");
      setKnowledgeEnvOverride(Boolean(fresh.knowledge?.environment_override));
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }, [gatewayBaseUrl, gatewayHealthPath, gatewayFallback, gatewayModel, thinkingMode, llmProvider, llmModel, llmBaseUrl, llmApiKey, temperature, maxTokens, embProvider, embModel, embDimension, embBatchSize, embBaseUrl, embApiKey, ragTopK, ragThreshold, ragTextVectorWeight, ragImageVectorWeight, ragBm25Weight, ragHybridCandidateTopK, ragRerankEnabled, ragRerankCandidateTopK, dbQaFullRowsTokenBudget, dbQaPreviewRowsTokenBudget, dbQaProfileTokenBudget, dbQaFullRowsHardRowCap, dbQaFullRowsHardColumnCap, dbQaMaxCellCharsForLlm, dbQaQueryTimeoutSeconds, dbQaResultStoreEnabled, dbQaResultStoreTtlHours, dbQaDefaultPageSize, dbQaMaxPageSize, dbQaExportEnabled, dbQaProfileEnabled, databaseMode, databaseHost, databasePort, databaseName, databaseUsername, databasePassword, mmModel, mmDimension, mmApiKey, knowledgeRootDir, kbIndexEnabled, kbVectorStore, kbMilvusUri, kbTextCollection, kbImageCollection, compRatio, contextSummaryTriggerTokens, toolContextEnabled, singleToolTriggerTokens, backgroundMinResultTokens, keepRecentToolResults, modelCallLimitEnabled, modelCallRunLimit, modelCallThreadLimit, modelCallExitBehavior, rubricEnabled, rubricMaxIterations, customRubricRulesEnabled, customRubricRules, goalsEnabled, goalMaxRounds, dockerEnabled, dockerOnUnavailable, dockerConnection, dockerContext, dockerUseCustomImage, dockerImage, dockerCpuLimit, dockerMemoryLimitMb, dockerPidsLimit, dockerNetworkEnabled, dockerDependencySetupEnabled, subagentItems, showToast]);

  const handleDatabaseModeChange = useCallback((mode: "bundled" | "external") => {
    setDatabaseMode(mode);
    setDatabaseHost("127.0.0.1");
    setDatabasePort("5432");
    if (mode === "bundled") {
      setDatabaseName("puddingclaw");
      setDatabaseUsername("puddingclaw");
      setDatabasePassword("puddingclaw");
    } else if (mode === "external") {
      setDatabaseName("postgres");
      setDatabaseUsername("pet");
      setDatabasePassword("");
    }
  }, []);

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
    setDatabaseTesting(true);
    setDatabaseTestResult(null);
    try {
      const result = await testDatabaseConnection(databaseConnectionPayload(false));
      if (result.success) {
        setDatabaseTestResult({ ok: true, msg: result.created ? "数据库已创建并连接成功" : `连接成功 · ${result.latency_ms}ms` });
        showToast("success", result.created ? "数据库已创建并连接成功" : "数据库连接成功");
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
        setDatabaseTestResult({ ok: true, msg: `数据库已创建并连接成功 · ${created.latency_ms}ms` });
        showToast("success", "数据库已创建并连接成功");
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
  }, [databaseConnectionPayload, databaseName, showToast]);

  const handleProbeDocker = useCallback(async () => {
    setDockerProbeStatus("loading");
    setDockerProbeDetail("");
    try {
      const result = await probeHarnessDocker({
        connection: dockerConnection,
        context: dockerContext,
      });
      setDockerProbeStatus(result.available ? "ok" : "error");
      setDockerProbeDetail(result.detail || (result.available ? "Docker 可用" : "Docker 不可用"));
    } catch (err) {
      setDockerProbeStatus("error");
      setDockerProbeDetail(err instanceof Error ? err.message : "Docker 探测失败");
    }
  }, [dockerConnection, dockerContext]);

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

  const handleRefreshGatewayModels = useCallback(async () => {
    setRefreshingModels(true);
    try {
      const fresh = await getSettings();
      setGatewayModels(fresh.ai_gateway.routed_models || []);
      setSettings(fresh);
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "刷新模型列表失败");
    } finally {
      setRefreshingModels(false);
    }
  }, [showToast]);

  const handleTestGateway = useCallback(async () => {
    setGatewayTesting(true);
    setGatewayTestResult(null);
    try {
      const result = await testConnection({
        type: "gateway",
        base_url: gatewayBaseUrl || "http://higress:8080/v1",
        health_path: gatewayHealthPath,
      });
      setGatewayTestResult({ ok: true, msg: `网关可用 (${result.latency_ms}ms)` });
    } catch (err) {
      setGatewayTestResult({ ok: false, msg: err instanceof Error ? err.message : "网关不可用" });
    } finally {
      setGatewayTesting(false);
    }
  }, [gatewayBaseUrl, gatewayHealthPath]);

  const handleTestLlm = useCallback(async () => {
    const key = llmApiKey || settings?.fallback_llm.api_key_masked || "";
    if (!key || key === "***") {
      setLlmTestResult({ ok: false, msg: "请先输入 API Key" });
      return;
    }
    setLlmTesting(true);
    setLlmTestResult(null);
    try {
      const result = await testConnection({
        type: "llm",
        provider: llmProvider,
        model: llmModel,
        base_url: llmBaseUrl,
        api_key: llmApiKey || "",
      });
      setLlmTestResult({ ok: true, msg: `连接成功 (${result.latency_ms}ms)` });
    } catch (err) {
      setLlmTestResult({ ok: false, msg: err instanceof Error ? err.message : "连接失败" });
    } finally {
      setLlmTesting(false);
    }
  }, [llmApiKey, llmProvider, llmModel, llmBaseUrl, settings]);

  const handleTestEmb = useCallback(async () => {
    const key = embApiKey || settings?.fallback_embedding.api_key_masked || "";
    if (!key || key === "***") {
      setEmbTestResult({ ok: false, msg: "请先输入 API Key" });
      return;
    }
    setEmbTesting(true);
    setEmbTestResult(null);
    try {
      const result = await testConnection({
        type: "embedding",
        provider: embProvider,
        model: embModel,
        base_url: embBaseUrl,
        api_key: embApiKey || "",
      });
      setEmbTestResult({ ok: true, msg: `连接成功 (${result.dimensions}维, ${result.latency_ms}ms)` });
    } catch (err) {
      setEmbTestResult({ ok: false, msg: err instanceof Error ? err.message : "连接失败" });
    } finally {
      setEmbTesting(false);
    }
  }, [embApiKey, embProvider, embModel, embBaseUrl, settings]);

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
        model: gatewayModels[0] || "",
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
  }, [gatewayModels]);

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
            <div className="flex-1 min-h-0 overflow-y-auto p-3">
              <Link
                href="/"
                className="flex items-center gap-2.5 px-3 py-2.5 mb-3 text-[13px] font-medium text-gray-700 bg-white/55 hover:bg-white/80 rounded-xl transition-all group"
              >
                <ArrowLeft className="w-4 h-4 text-gray-500 group-hover:text-gray-700 transition-colors" />
                返回应用
              </Link>
              <div className="space-y-0.5">
                {CATEGORIES.map((cat) => {
                  const Icon = cat.icon;
                  const active = category === cat.key;
                  return (
                    <button
                      key={cat.key}
                      onClick={() => setCategory(cat.key)}
                      className={`w-full flex items-center gap-2.5 px-3 py-2 text-[12px] rounded-lg transition-all text-left ${
                        active
                          ? "bg-white/80 text-gray-900 font-medium shadow-sm"
                          : "text-gray-500 hover:bg-white/55 hover:text-gray-800"
                      }`}
                    >
                      <Icon className="w-3.5 h-3.5" style={active ? { color: cat.color } : {}} />
                      {cat.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </div>

        {/* Right: Settings Form */}
        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto px-8 pb-8 pt-6">
            <div className={`${
              category === "ai" || category === "databaseQa" ? "max-w-4xl" : category === "harness" || category === "project" ? "max-w-6xl" : "max-w-2xl"
            } mx-auto space-y-6`}>
            {category === "ai" && (
              <>
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <h1 className="text-[22px] font-semibold tracking-tight text-gray-900">AI 网关</h1>
                    <p className="mt-1 text-[12px] text-gray-500">
                      管理请求经过哪里、使用哪个模型，以及每一层的访问凭证。
                    </p>
                  </div>
                  <div className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] font-medium ${
                    capabilities?.ai_gateway.available
                      ? "bg-emerald-50 text-emerald-700"
                      : "bg-amber-50 text-amber-700"
                  }`}>
                    <span className="h-1.5 w-1.5 rounded-full bg-current" />
                    {capabilities?.ai_gateway.available ? "Gateway 模式" : "Provider 直连"}
                  </div>
                </div>

                <div className="grid grid-cols-[1fr_28px_1fr_28px_1fr] items-center rounded-2xl border border-black/[0.055] bg-white p-4 shadow-sm">
                  <RouteNode title="PuddingClaw" detail="ModelClient · 统一入口" status="运行中" tone="green" />
                  <Route className="mx-auto h-4 w-4 text-gray-300" />
                  <RouteNode
                    title="Higress Gateway"
                    detail={capabilities?.ai_gateway.available ? (gatewayBaseUrl || "http://higress:8080/v1") : "未探测到，失败时回退 Provider 直连"}
                    status={capabilities?.ai_gateway.available ? "已接入" : "未接入"}
                    tone={capabilities?.ai_gateway.available ? "green" : "amber"}
                  />
                  <Route className="mx-auto h-4 w-4 text-gray-300" />
                  <RouteNode title={gatewayModel} detail="网关模型" status="主模型" tone="blue" />
                </div>

                <div className="rounded-2xl border border-black/[0.055] bg-white p-5 shadow-sm">
                  <div className="mb-5 flex items-center justify-between gap-4">
                    <div className="flex items-center gap-3">
                      <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[#002fa7]/8 text-[#002fa7]">
                        <Network className="h-4 w-4" />
                      </div>
                      <div>
                        <h2 className="text-[14px] font-semibold text-gray-800">AI Gateway</h2>
                        <p className="mt-0.5 text-[11px] text-gray-500">Higress · OpenAI-compatible endpoint</p>
                        {gatewayEnvironmentOverride && (
                          <p className="mt-1 text-[10px] font-medium text-amber-600">当前值由环境变量覆盖，页面保存不会改变运行时覆盖值</p>
                        )}
                      </div>
                    </div>
                    <a
                      href="http://localhost:8001"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex shrink-0 items-center gap-1.5 rounded-lg bg-[#002fa7]/10 px-3 py-2 text-[11px] font-medium text-[#002fa7] transition-colors hover:bg-[#002fa7]/15"
                    >
                      <ExternalLink className="h-3.5 w-3.5" />
                      打开 Console
                    </a>
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <FormField label="Gateway 覆盖地址（可选）">
                      <input value={gatewayBaseUrl} onChange={(e) => setGatewayBaseUrl(e.target.value)} className="form-input" placeholder="留空则自动探测 http://higress:8080/v1" />
                    </FormField>
                    <FormField label="健康检查路径">
                      <input value={gatewayHealthPath} onChange={(e) => setGatewayHealthPath(e.target.value)} className="form-input" placeholder="/health" />
                    </FormField>
                    <FormField label="失败策略">
                      <label className="flex h-[34px] items-center justify-between rounded-lg border border-black/[0.08] bg-white/70 px-3 text-[11px] text-gray-600">
                        首个 token 前失败时回退 Provider 直连
                        <input type="checkbox" checked={gatewayFallback} onChange={(e) => setGatewayFallback(e.target.checked)} className="accent-[#002fa7]" />
                      </label>
                    </FormField>
                    <div className="flex items-center justify-between gap-4 rounded-xl border border-black/[0.06] bg-white/55 px-3 py-2.5">
                      <p className="text-[10px] leading-relaxed text-gray-500">
                        Higress 只负责代理、Token 统计与模型切换；模型访问始终使用对应 Provider Key。
                      </p>
                      <button onClick={handleTestGateway} disabled={gatewayTesting} className="flex shrink-0 items-center gap-1.5 rounded-lg bg-[#002fa7]/10 px-3 py-2 text-[11px] font-medium text-[#002fa7] transition-colors hover:bg-[#002fa7]/15 disabled:opacity-50">
                        {gatewayTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
                        测试网关
                      </button>
                    </div>
                    {gatewayTestResult && <div className="col-span-2"><ConnectionResult result={gatewayTestResult} /></div>}
                  </div>

                  {/* Higress Routed Models */}
                  {gatewayModels.length > 0 && (
                    <div className="mt-5 border-t border-black/[0.06] pt-5">
                      <div className="flex items-center gap-3 mb-4">
                        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
                          <Route className="h-3.5 w-3.5" />
                        </div>
                        <div>
                          <h3 className="text-[13px] font-semibold text-gray-800">网关模型</h3>
                          <p className="mt-0.5 text-[11px] text-gray-500">当前：{gatewayModel}，点击下方路由切换，保存后生效</p>
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {gatewayModels.map((model) => {
                          const active = model === gatewayModel;
                          return (
                            <button
                              key={model}
                              onClick={() => setGatewayModel(model)}
                              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-medium transition-all border ${
                                active
                                  ? "bg-[#002fa7] text-white border-[#002fa7] shadow-sm"
                                  : "bg-white/70 text-gray-600 border-black/[0.06] hover:bg-white hover:border-[#002fa7]/30"
                              }`}
                              title={active ? "当前网关模型已匹配此路由" : "点击将网关模型设为此值"}
                            >
                              {active && <CheckCircle2 className="h-3 w-3" />}
                              {model}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>

                {/* Thinking Mode */}
                <div className="mt-5 rounded-xl border border-black/[0.06] bg-white/55 px-4 py-3.5">
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[#002fa7]/8 text-[#002fa7]">
                        <Brain className="h-4 w-4" />
                      </div>
                      <div className="min-w-0">
                        <h3 className="text-[13px] font-semibold text-gray-800">思考模式</h3>
                        <p className="mt-0.5 text-[11px] text-gray-500 truncate">
                          {thinkingMode
                            ? "使用 deepseek-v4-pro 并输出思维链"
                            : "使用默认模型，不输出思维链"}
                        </p>
                      </div>
                    </div>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={thinkingMode}
                      aria-label="启用思考模式"
                      onClick={() => setThinkingMode(!thinkingMode)}
                      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#002fa7]/40 ${
                        thinkingMode ? "bg-[#002fa7]" : "bg-gray-300"
                      }`}
                    >
                      <span
                        className={`pointer-events-none mt-0.5 inline-block h-5 w-5 rounded-full bg-white shadow-sm transition-transform ${
                          thinkingMode ? "translate-x-[22px]" : "translate-x-0.5"
                        }`}
                      />
                    </button>
                  </div>
                </div>
              </>
            )}

            {category === "project" && (
              <div className="space-y-5">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <h1 className="text-[22px] font-semibold tracking-tight text-gray-900">项目上下文</h1>
                    <p className="mt-1 text-[12px] text-gray-500">
                      编辑当前项目注入 DeepAgents system prompt 的项目级上下文。
                    </p>
                  </div>
                  {currentProject && (
                    <span className="rounded-full bg-[#002fa7]/8 px-3 py-1 text-[11px] font-medium text-[#002fa7]">
                      {currentProject.name}
                    </span>
                  )}
                </div>

                {!currentProjectId ? (
                  <div className="rounded-2xl border border-dashed border-black/[0.08] bg-white/70 p-8 text-center shadow-sm">
                    <FileText className="mx-auto h-8 w-8 text-gray-300" />
                    <h2 className="mt-3 text-[14px] font-semibold text-gray-800">还没有选择项目</h2>
                    <p className="mx-auto mt-2 max-w-md text-[12px] leading-relaxed text-gray-500">
                      先在侧边栏或输入框添加本地项目。PuddingClaw 会在项目根目录创建
                      <code className="mx-1 rounded bg-gray-100 px-1 py-0.5">.puddingclaw/PROJECT_CONTEXT.md</code>
                      ，之后这里就能编辑。
                    </p>
                  </div>
                ) : (
                  <SettingsCard title="PROJECT_CONTEXT.md" icon={FileText} color="#002fa7">
                    <div className="mb-4 grid gap-3 lg:grid-cols-[1fr_auto]">
                      <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">文件位置</p>
                        <p className="mt-1 break-all font-mono text-[11px] text-gray-600">
                          {projectContextDoc?.path || `${currentProject?.path || ""}/.puddingclaw/PROJECT_CONTEXT.md`}
                        </p>
                        <p className="mt-2 text-[11px] text-gray-400">
                          {projectContextDoc?.is_project_local
                            ? "项目本地副本，随项目迁移和版本管理。"
                            : "当前使用默认模板；保存后会写入项目本地副本。"}
                        </p>
                      </div>
                      <button
                        type="button"
                        onClick={handleSaveProjectContext}
                        disabled={projectContextSaving || projectContextLoading}
                        className="inline-flex h-full min-h-[72px] items-center justify-center gap-2 rounded-xl bg-[#002fa7] px-5 text-[12px] font-semibold text-white shadow-sm transition-colors hover:bg-[#00298f] disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {projectContextSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                        保存项目上下文
                      </button>
                    </div>

                    <textarea
                      value={projectContextContent}
                      onChange={(event) => setProjectContextContent(event.target.value)}
                      disabled={projectContextLoading}
                      spellCheck={false}
                      className="min-h-[520px] w-full resize-y rounded-xl border border-black/[0.08] bg-white/80 p-4 font-mono text-[12px] leading-relaxed text-gray-800 outline-none transition-colors focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/8 disabled:opacity-60"
                      placeholder={projectContextLoading ? "正在加载项目上下文..." : "写入当前项目的业务背景、架构约束、目录约定和稳定决策。"}
                    />

                    <div className="mt-3 rounded-xl border border-amber-100 bg-amber-50/60 px-3.5 py-3 text-[11px] leading-relaxed text-amber-700">
                      这里不要维护 skills 列表或工具 schema；skills 由 SkillsMiddleware 运行时注入，tools 通过 API tools 字段进入模型。
                    </div>
                  </SettingsCard>
                )}
              </div>
            )}

            {category === "databaseQa" && (
              <div className="space-y-5">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <h1 className="text-[22px] font-semibold tracking-tight text-gray-900">智能问数</h1>
                    <p className="mt-1 text-[12px] text-gray-500">
                      控制数据库问数的上下文预算、结果持久化、分页和 Trace 可观测性。
                    </p>
                  </div>
                  <button
                    onClick={handleSave}
                    disabled={saving}
                    className="flex items-center gap-2 rounded-xl bg-[#002fa7] px-4 py-2.5 text-[12px] font-medium text-white shadow-sm transition-all hover:bg-[#002fa7]/90 disabled:opacity-50"
                  >
                    {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    保存设置
                  </button>
                </div>
                <SettingsCard title="模型上下文预算" icon={Database} color="#002fa7">
                  <div className="grid grid-cols-2 gap-4">
                    <FormField label="完整明细 Token 预算">
                      <input value={dbQaFullRowsTokenBudget} onChange={(e) => setDbQaFullRowsTokenBudget(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                    <FormField label="预览明细 Token 预算">
                      <input value={dbQaPreviewRowsTokenBudget} onChange={(e) => setDbQaPreviewRowsTokenBudget(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                    <FormField label="Profile Token 预算">
                      <input value={dbQaProfileTokenBudget} onChange={(e) => setDbQaProfileTokenBudget(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                    <FormField label="单元格最大字符数">
                      <input value={dbQaMaxCellCharsForLlm} onChange={(e) => setDbQaMaxCellCharsForLlm(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                  </div>
                  <p className="mt-3 text-[11px] leading-relaxed text-gray-500">
                    预算越大，明细问题越可能直接完整回答；同时会增加模型上下文占用和延迟。
                  </p>
                </SettingsCard>

                <SettingsCard title="完整明细保护" icon={ShieldCheck} color="#0f172a">
                  <div className="grid grid-cols-2 gap-4">
                    <FormField label="完整明细最大行数">
                      <input value={dbQaFullRowsHardRowCap} onChange={(e) => setDbQaFullRowsHardRowCap(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                    <FormField label="完整明细最大列数">
                      <input value={dbQaFullRowsHardColumnCap} onChange={(e) => setDbQaFullRowsHardColumnCap(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                    <FormField label="SQL 执行超时（秒）">
                      <input value={dbQaQueryTimeoutSeconds} onChange={(e) => setDbQaQueryTimeoutSeconds(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                  </div>
                </SettingsCard>

                <SettingsCard title="持久化与分页" icon={FileText} color="#10b981">
                  <div className="grid grid-cols-2 gap-4">
                    <FormField label="默认分页大小">
                      <input value={dbQaDefaultPageSize} onChange={(e) => setDbQaDefaultPageSize(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                    <FormField label="最大分页大小">
                      <input value={dbQaMaxPageSize} onChange={(e) => setDbQaMaxPageSize(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                    <FormField label="结果保留时间（小时）">
                      <input value={dbQaResultStoreTtlHours} onChange={(e) => setDbQaResultStoreTtlHours(e.target.value)} className="form-input" inputMode="numeric" />
                    </FormField>
                    <div className="grid gap-2">
                      <ToggleRow
                        label="持久化结果集"
                        description="关闭后，大明细不会落盘，也不会生成 result_id、分页读取和导出入口。"
                        checked={dbQaResultStoreEnabled}
                        onChange={setDbQaResultStoreEnabled}
                      />
                      <ToggleRow
                        label="生成 Profile"
                        description="为截断明细生成分布摘要，帮助模型避免从预览行误判。"
                        checked={dbQaProfileEnabled}
                        onChange={setDbQaProfileEnabled}
                      />
                      <ToggleRow
                        label="允许导出"
                        description="控制查询结果页的 CSV 导出按钮和后端导出 API。"
                        checked={dbQaExportEnabled}
                        onChange={setDbQaExportEnabled}
                      />
                    </div>
                  </div>
                  <p className="mt-3 text-[11px] leading-relaxed text-gray-500">
                    超出上下文预算的明细会落盘到 backend/data/database-query-results，并通过 Trace 暴露 result_id、过期时间和分页动作。
                  </p>
                </SettingsCard>
              </div>
            )}

            {/* Fallback Settings */}
            {category === "ai" && (
              <SettingsCard title="Fallback 直连配置" icon={Bot} color="#6b7280">
                <div className="rounded-xl border border-amber-100/80 bg-amber-50/50 px-3.5 py-3 mb-4">
                  <p className="text-[11px] leading-relaxed text-amber-700">
                    <strong>说明：</strong>Higress 可用时，LLM / Embedding 请求会优先经过网关路由；以下配置仅在网关探测失败或 fallback 时生效。
                  </p>
                </div>

                <h3 className="mb-3 text-[13px] font-semibold text-gray-700">LLM 模型</h3>
                <FormField label="Provider">
                  <select
                    value={llmProvider}
                    onChange={(e) => {
                      setLlmProvider(e.target.value);
                      const p = LLM_PROVIDERS.find((p) => p.value === e.target.value);
                      if (p && p.baseUrl) setLlmBaseUrl(p.baseUrl);
                    }}
                    className="form-select"
                  >
                    {LLM_PROVIDERS.map((p) => (
                      <option key={p.value} value={p.value}>{p.label}</option>
                    ))}
                  </select>
                </FormField>
                <FormField label="Model">
                  <input
                    type="text"
                    value={llmModel}
                    onChange={(e) => setLlmModel(e.target.value)}
                    className="form-input"
                    placeholder="deepseek-chat"
                  />
                </FormField>
                <FormField label="Base URL">
                  <input
                    type="text"
                    value={llmBaseUrl}
                    onChange={(e) => setLlmBaseUrl(e.target.value)}
                    className="form-input"
                    placeholder="https://api.deepseek.com"
                  />
                </FormField>
                <FormField label="API Key">
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <input
                        type={showLlmKey ? "text" : "password"}
                        value={llmApiKey}
                        onChange={(e) => setLlmApiKey(e.target.value)}
                        className="form-input pr-8"
                        placeholder={llmApiKeyMasked || "sk-..."}
                      />
                      <button
                        onClick={() => setShowLlmKey((v) => !v)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                      >
                        {showLlmKey ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                      </button>
                    </div>
                    <button
                      onClick={handleTestLlm}
                      disabled={llmTesting}
                      className="px-3 py-1.5 text-[11px] font-medium rounded-lg bg-[#002fa7]/10 text-[#002fa7] hover:bg-[#002fa7]/20 transition-colors disabled:opacity-50 flex items-center gap-1.5 shrink-0"
                    >
                      {llmTesting ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
                      测试连接
                    </button>
                  </div>
                  {llmTestResult && (
                    <div className={`mt-1.5 flex items-center gap-1 text-[11px] ${llmTestResult.ok ? "text-emerald-600" : "text-red-500"}`}>
                      {llmTestResult.ok ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                      {llmTestResult.msg}
                    </div>
                  )}
                </FormField>
                <FormField label={`Temperature: ${temperature}`}>
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    value={temperature}
                    onChange={(e) => setTemperature(parseFloat(e.target.value))}
                    className="w-full accent-[#002fa7]"
                  />
                  <div className="flex justify-between text-[10px] text-gray-400 mt-0.5">
                    <span>精确 (0)</span>
                    <span>创意 (2)</span>
                  </div>
                </FormField>
                <FormField label="Max Tokens">
                  <input
                    type="number"
                    min="256"
                    max="128000"
                    value={maxTokens}
                    onChange={(e) => setMaxTokens(parseInt(e.target.value) || 4096)}
                    className="form-input"
                  />
                </FormField>

                <div className="my-5 border-t border-black/[0.06]" />
                <h3 className="mb-3 text-[13px] font-semibold text-gray-700">Embedding 模型</h3>
                <FormField label="Provider">
                  <select
                    value={embProvider}
                    onChange={(e) => {
                      setEmbProvider(e.target.value);
                      const p = EMBEDDING_PROVIDERS.find((p) => p.value === e.target.value);
                      if (p && p.baseUrl) setEmbBaseUrl(p.baseUrl);
                    }}
                    className="form-select"
                  >
                    {EMBEDDING_PROVIDERS.map((p) => (
                      <option key={p.value} value={p.value}>{p.label}</option>
                    ))}
                  </select>
                </FormField>
                <FormField label="Model">
                  <input
                    type="text"
                    value={embModel}
                    onChange={(e) => setEmbModel(e.target.value)}
                    className="form-input"
                    placeholder="text-embedding-v3"
                  />
                </FormField>
                <div className="grid gap-4 md:grid-cols-2">
                  <FormField label="文本向量维度">
                    <input
                      type="number"
                      min="1"
                      value={embDimension}
                      onChange={(e) => setEmbDimension(e.target.value)}
                      className="form-input"
                      placeholder="1024"
                    />
                  </FormField>
                  <FormField label="文本批量大小">
                    <input
                      type="number"
                      min="1"
                      value={embBatchSize}
                      onChange={(e) => setEmbBatchSize(e.target.value)}
                      className="form-input"
                      placeholder="20"
                    />
                    <p className="mt-1 text-[10px] text-gray-400">
                      DashScope 文本向量建议不超过 20。
                    </p>
                  </FormField>
                </div>
                <FormField label="Base URL">
                  <input
                    type="text"
                    value={embBaseUrl}
                    onChange={(e) => setEmbBaseUrl(e.target.value)}
                    className="form-input"
                  />
                </FormField>
                <FormField label="API Key">
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <input
                        type={showEmbKey ? "text" : "password"}
                        value={embApiKey}
                        onChange={(e) => setEmbApiKey(e.target.value)}
                        className="form-input pr-8"
                        placeholder={embApiKeyMasked || "sk-..."}
                      />
                      <button
                        onClick={() => setShowEmbKey((v) => !v)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                      >
                        {showEmbKey ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                      </button>
                    </div>
                    <button
                      onClick={handleTestEmb}
                      disabled={embTesting}
                      className="px-3 py-1.5 text-[11px] font-medium rounded-lg bg-amber-500/10 text-amber-600 hover:bg-amber-500/20 transition-colors disabled:opacity-50 flex items-center gap-1.5 shrink-0"
                    >
                      {embTesting ? <Loader2 className="w-3 h-3 animate-spin" /> : <Zap className="w-3 h-3" />}
                      测试连接
                    </button>
                  </div>
                  {embTestResult && (
                    <div className={`mt-1.5 flex items-center gap-1 text-[11px] ${embTestResult.ok ? "text-emerald-600" : "text-red-500"}`}>
                      {embTestResult.ok ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                      {embTestResult.msg}
                    </div>
                  )}
                </FormField>
              </SettingsCard>
            )}

            {/* RAG Settings */}
            {category === "rag" && (
              <SettingsCard title="RAG 检索设置" icon={Database} color="#002fa7">
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
            )}

            {/* Knowledge Base Settings */}
            {category === "knowledge" && (
              <div className="space-y-5">
                <SettingsCard title="知识库数据库" icon={Database} color="#0f172a">
                  <div className="rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-3">
                    <p className="text-[11px] leading-relaxed text-slate-600">
                      用来记录知识库里的文件、导入进度和检索信息。一般不用手动改；启动脚本会自动选择本机数据库或 PuddingClaw 内置数据库。
                    </p>
                  </div>

                  {databaseEnvOverride ? (
                    <div className="rounded-xl border border-amber-100 bg-amber-50/60 px-3.5 py-3 text-[11px] text-amber-700">
                      当前数据库连接由启动环境覆盖；保存这里的配置后，不会改变当前运行时连接。
                    </div>
                  ) : null}

                  <div className="grid gap-4 md:grid-cols-2">
                    <FormField label="模式">
                      <select
                        value={databaseMode}
                        onChange={(e) => handleDatabaseModeChange(e.target.value as "bundled" | "external")}
                        className="form-select"
                      >
                        <option value="bundled">PuddingClaw 内置 PostgreSQL</option>
                        <option value="external">本机 PostgreSQL</option>
                      </select>
                    </FormField>
                    <FormField label="本机端口">
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
                        placeholder="puddingclaw"
                      />
                    </FormField>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={handleTestDatabase}
                      disabled={databaseEnvOverride || databaseTesting}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-gray-950 px-3 py-2 text-[11px] font-medium text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {databaseTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
                      测试连接
                    </button>
                    <span className="text-[11px] text-gray-400">
                      当前来源：{databaseConfiguredBy}
                    </span>
                  </div>
                  {databaseTestResult ? (
                    <div
                      className={`rounded-xl border px-3.5 py-3 text-[11px] ${
                        databaseTestResult.ok
                          ? "border-emerald-100 bg-emerald-50 text-emerald-700"
                          : "border-red-100 bg-red-50 text-red-700"
                      }`}
                    >
                      {databaseTestResult.msg}
                    </div>
                  ) : null}
                </SettingsCard>

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
                        placeholder="/Users/pet/Documents/PuddingClawKnowledge（留空使用 backend/knowledge）"
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

                <SettingsCard title="多模态 Embedding" icon={Database} color="#002fa7">
                  <div className="rounded-xl border border-[#002fa7]/10 bg-[#002fa7]/[0.04] px-3.5 py-3">
                    <p className="text-[11px] leading-relaxed text-[#002fa7]">
                      图文混排 PDF 默认使用 DashScope Qwen-VL Embedding 直连。通常只需要配置 API Key；如果 Higress 已有同一 DashScope Key，后端会自动复用。
                    </p>
                  </div>
                  <div className="grid gap-4 md:grid-cols-2">
                    <FormField label="Model">
                      <input value={mmModel} onChange={(e) => setMmModel(e.target.value)} className="form-input" placeholder="qwen2.5-vl-embedding" />
                    </FormField>
                    <FormField label="Dimension">
                      <input value={mmDimension} onChange={(e) => setMmDimension(e.target.value)} className="form-input" placeholder="1024" />
                    </FormField>
                    <FormField label="并发数">
                      <input value={mmConcurrency} onChange={(e) => setMmConcurrency(e.target.value)} className="form-input" placeholder="10" />
                      <p className="mt-1 text-[11px] leading-relaxed text-gray-400">
                        DashScope 多模态接口不支持同类型批量输入，这里控制同时发起多少个单条请求。
                      </p>
                    </FormField>
                    <FormField label="API Key（可选）">
                      <div className="relative">
                        <input
                          type={showMmKey ? "text" : "password"}
                          value={mmApiKey}
                          onChange={(e) => setMmApiKey(e.target.value)}
                          className="form-input pr-8"
                          placeholder={mmApiKeyMasked || "可留空：自动复用 Higress DashScope Key"}
                        />
                        <button
                          type="button"
                          onClick={() => setShowMmKey((v) => !v)}
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                        >
                          {showMmKey ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                        </button>
                      </div>
                    </FormField>
                  </div>
                </SettingsCard>

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
              </div>
            )}

            {/* Agent / Harness Config */}
            {category === "harness" && (
              <div className="flex flex-col lg:flex-row gap-5">
                {/* Left: harness category sidebar */}
                <aside className="flex w-full flex-col gap-4 lg:sticky lg:top-6 lg:w-56 lg:self-start lg:shrink-0">
                  <div>
                    <h1 className="text-[18px] font-semibold text-gray-900">Harness 设置</h1>
                    <p className="mt-1 text-[11px] text-gray-500">管理 Agent 编排能力</p>
                  </div>

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
                              gatewayModels={gatewayModels}
                              refreshingModels={refreshingModels}
                              onChange={updateSubAgentItem}
                              onRefreshModels={handleRefreshGatewayModels}
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
                              当模型上下文达到阈值时，由内置摘要中间件压缩历史；摘要输入预算由后端根据当前模型上下文窗口自动计算。
                            </p>
                          </div>
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
                        </div>

                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <div>
                              <p className="text-[13px] font-semibold text-gray-900">工具上下文压缩</p>
                              <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                                开启时注册 DeepAgents Tool Context 中间件。关闭后不注册中间件、不创建新任务，模型直接使用原始工具结果。
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

                          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
                            <FormField label="执行中单条阈值">
                              <input
                                type="number"
                                min={1000}
                                max={100000}
                                step={1000}
                                value={singleToolTriggerTokens}
                                onChange={(e) => setSingleToolTriggerTokens(e.target.value)}
                                className="form-input"
                                disabled={!toolContextEnabled}
                              />
                              <p className="mt-1 text-[10px] text-gray-400">默认 8,000 tokens；只保护超限的单条结果。</p>
                            </FormField>
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
                            <FormField label="保留最近完整结果">
                              <input
                                type="number"
                                min={1}
                                max={100}
                                value={keepRecentToolResults}
                                onChange={(e) => setKeepRecentToolResults(e.target.value)}
                                className="form-input"
                                disabled={!toolContextEnabled}
                              />
                              <p className="mt-1 text-[10px] text-gray-400">默认 12 条已完成 Tool Result 不参与事后压缩。</p>
                            </FormField>
                          </div>
                        </div>

                        <SpecPreview
                          spec={{
                            compression: {
                              deepagents: {
                                summarization: {
                                  trigger_tokens: positiveIntOrNull(contextSummaryTriggerTokens) ?? 200000,
                                },
                                tool_context: {
                                  enabled: toolContextEnabled,
                                  single_tool_trigger_tokens: positiveIntOrNull(singleToolTriggerTokens) ?? 8000,
                                  background_min_result_tokens: positiveIntOrNull(backgroundMinResultTokens) ?? 1000,
                                  keep_recent_tool_results: positiveIntOrNull(keepRecentToolResults) ?? 12,
                                },
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
                              <p className="text-[13px] font-semibold text-gray-900">Run Rubric 验收</p>
                              <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                                Rubric 属于 Run。即使未开启 Goal，有明确产物或分析结果的 Run 仍可自动验收。
                              </p>
                            </div>
                            <SwitchButton
                              checked={rubricEnabled}
                              onChange={setRubricEnabled}
                              ariaLabel="启用 Run Rubric"
                            />
                          </div>
                          <div className="mt-4 max-w-xs">
                            <FormField label="单 Run 最大修正轮数">
                              <input
                                type="number"
                                min={1}
                                max={20}
                                value={rubricMaxIterations}
                                onChange={(event) => setRubricMaxIterations(event.target.value)}
                                className="form-input"
                                disabled={!rubricEnabled}
                              />
                            </FormField>
                          </div>
                        </div>

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
                        </div>
                      </div>
                    </SettingsCard>
                  </section>

                  <section id="harness-section-sandbox" className="scroll-mt-6">
                    <SettingsCard title="终端与沙箱" icon={Box} color="#002fa7">
                      <div className="space-y-4">
                        <div className="rounded-xl border border-black/[0.06] bg-white/55 px-3.5 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <div>
                              <p className="text-[13px] font-semibold text-gray-900">Docker 项目沙箱</p>
                              <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
                                每个项目复用一个容器，项目目录挂载到 /workspace。Docker 只提供隔离，命令仍经过权限管线。
                              </p>
                            </div>
                            <SwitchButton
                              checked={dockerEnabled}
                              onChange={setDockerEnabled}
                              ariaLabel="启用 Docker 项目沙箱"
                            />
                          </div>

                          <div className="mt-4 grid gap-3 sm:grid-cols-2">
                            <FormField label="Docker connection / DOCKER_HOST">
                              <input value={dockerConnection} onChange={(event) => setDockerConnection(event.target.value)} className="form-input" placeholder="留空使用本机默认" />
                            </FormField>
                            <FormField label="Docker context">
                              <input value={dockerContext} onChange={(event) => setDockerContext(event.target.value)} className="form-input" placeholder="留空使用当前 context" />
                            </FormField>
                            <FormField label="运行时镜像">
                              <div className="rounded-xl border border-black/[0.06] bg-slate-50 px-3 py-2.5">
                                <p className="text-[12px] font-semibold text-slate-800">PuddingClaw 托管镜像</p>
                                <p className="mt-0.5 text-[10px] text-slate-500">Python 3.12 + Node.js 22</p>
                              </div>
                            </FormField>
                            <FormField label="Docker 不可用时">
                              <select value={dockerOnUnavailable} onChange={(event) => setDockerOnUnavailable(event.target.value === "deny" ? "deny" : "fallback")} className="form-select">
                                <option value="fallback">降级到受控 Host Terminal</option>
                                <option value="deny">拒绝命令执行</option>
                              </select>
                            </FormField>
                            <FormField label="CPU 核数">
                              <select value={dockerCpuLimit} onChange={(event) => setDockerCpuLimit(event.target.value)} className="form-select">
                                {DOCKER_CPU_OPTIONS.map((value) => (
                                  <option key={value} value={value}>{value} 核</option>
                                ))}
                              </select>
                            </FormField>
                            <FormField label="内存限额">
                              <select value={dockerMemoryLimitMb} onChange={(event) => setDockerMemoryLimitMb(event.target.value)} className="form-select">
                                {DOCKER_MEMORY_OPTIONS.map((option) => (
                                  <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                              </select>
                            </FormField>
                            <FormField label="进程数上限">
                              <input type="number" min={16} value={dockerPidsLimit} onChange={(event) => setDockerPidsLimit(event.target.value)} className="form-input" />
                            </FormField>
                            <label className="flex items-center gap-2 pt-6 text-[12px] text-gray-600">
                              <input type="checkbox" checked={dockerNetworkEnabled} onChange={(event) => setDockerNetworkEnabled(event.target.checked)} />
                              允许容器常驻网络
                            </label>
                          </div>

                          <div className="mt-3 rounded-xl border border-black/[0.07] bg-slate-50/80 px-3.5 py-3">
                            <label className="flex items-start gap-2 text-[12px] text-slate-800">
                              <input
                                type="checkbox"
                                checked={dockerUseCustomImage}
                                onChange={(event) => setDockerUseCustomImage(event.target.checked)}
                                className="mt-0.5"
                              />
                              <span>
                                <span className="font-semibold">使用自定义镜像（高级）</span>
                                <span className="mt-0.5 block text-[10px] leading-4 text-slate-500">
                                  默认使用 PuddingClaw 托管镜像。自定义镜像必须同时提供 Python 与 Node.js 基础运行时。
                                </span>
                              </span>
                            </label>
                            {dockerUseCustomImage && (
                              <div className="mt-3 pl-7">
                                <FormField label="自定义镜像引用">
                                  <input
                                    value={dockerImage}
                                    onChange={(event) => setDockerImage(event.target.value)}
                                    className="form-input"
                                    placeholder="例如 my-company/puddingclaw-sandbox:latest"
                                  />
                                </FormField>
                              </div>
                            )}
                          </div>

                          <p className="mt-3 rounded-lg bg-blue-50 px-3 py-2 text-[10px] leading-relaxed text-blue-700">
                            默认托管镜像只提供 Python 3.12 + pip 与 Node.js 22 + npm/corepack。
                            默认不安装项目依赖；第三方 Skill 执行中缺包时才请求 package/network 权限并动态安装。
                            自定义镜像填写的是本机 Docker tag 或 registry image reference，不需要上传镜像文件。
                            未开启常驻网络时，批准的安装命令只会临时联网，结束后自动断开。
                          </p>

                          <div className="mt-3 rounded-xl border border-amber-200/70 bg-amber-50/70 px-3.5 py-3">
                            <label className="flex items-start gap-2 text-[12px] text-amber-950">
                              <input
                                type="checkbox"
                                checked={dockerDependencySetupEnabled}
                                onChange={(event) => setDockerDependencySetupEnabled(event.target.checked)}
                                className="mt-0.5"
                              />
                              <span>
                                <span className="font-semibold">按项目 lockfile 准备依赖（高级）</span>
                                <span className="mt-0.5 block text-[10px] leading-4 text-amber-800">
                                  默认关闭以保持纯净沙箱。仅当确实需要运行项目脚本或测试时主动开启。
                                </span>
                              </span>
                            </label>
                          </div>

                          <div className="mt-4 flex flex-wrap items-center gap-3">
                            <button
                              type="button"
                              onClick={() => void handleProbeDocker()}
                              disabled={dockerProbeStatus === "loading"}
                              className="rounded-xl border border-black/[0.08] bg-white px-3 py-2 text-[12px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-60"
                            >
                              {dockerProbeStatus === "loading" ? "正在探测…" : "探测 Docker"}
                            </button>
                            {dockerProbeStatus !== "idle" && (
                              <span className={`text-[11px] ${dockerProbeStatus === "ok" ? "text-emerald-700" : "text-rose-600"}`}>
                                {dockerProbeDetail}
                              </span>
                            )}
                          </div>
                        </div>
                        <p className="rounded-xl bg-amber-50 px-3 py-2 text-[11px] leading-5 text-amber-800">
                          未启用或不可用时，Restricted Host 只是 best-effort 降级，不会在 UI/Trace 中宣称为真沙箱。sudo、Docker 控制和宿主 workspace 外路径始终硬拒绝。
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
                          gatewayModels={gatewayModels}
                          refreshingModels={refreshingModels}
                          onChange={updateSubAgentItem}
                          onRefreshModels={handleRefreshGatewayModels}
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
            {/* Memory Editor */}
            {category === "memory" && (
              <div className="h-[calc(100vh-140px)]">
                <MemoryEditor />
              </div>
            )}

            {/* Advanced Settings */}
            {category === "advanced" && (
              <SettingsCard title="高级设置" icon={Sliders} color="#6b7280">
                <FormField label={`压缩比例: ${Math.round(compRatio * 100)}%`}>
                  <input
                    type="range"
                    min="0.2"
                    max="0.8"
                    step="0.05"
                    value={compRatio}
                    onChange={(e) => setCompRatio(parseFloat(e.target.value))}
                    className="w-full accent-gray-500"
                  />
                  <div className="flex justify-between text-[10px] text-gray-400 mt-0.5">
                    <span>少压缩 (20%)</span>
                    <span>多压缩 (80%)</span>
                  </div>
                </FormField>
              </SettingsCard>
            )}

            {/* System Status */}
            {category === "system" && (
              <SettingsCard title="系统状态" icon={Activity} color="#002fa7">
                <CapabilitiesStatus refreshIntervalMs={30000} onChange={setCapabilities} />
              </SettingsCard>
            )}

            {/* Save Button */}
            <div className="flex justify-end pt-2 pb-8">
              <button
                onClick={handleSave}
                disabled={saving}
                className="flex items-center gap-2 px-5 py-2.5 text-[13px] font-medium text-white bg-[#002fa7] hover:bg-[#001f7a] rounded-xl transition-all active:scale-95 disabled:opacity-50 shadow-lg shadow-[#002fa7]/15"
              >
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                保存设置
              </button>
            </div>
          </div>
        </div>
      </main>
    </div>

    {/* Toast */}
    {toast && (
      <div className={`fixed bottom-6 right-6 flex items-center gap-2 px-4 py-2.5 rounded-xl text-[13px] font-medium shadow-lg animate-fade-in ${
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
  gatewayModels,
  refreshingModels,
  onChange,
  onRefreshModels,
  onDelete,
  onClose,
  isMobile,
}: {
  index: number;
  item: SubAgentItem;
  gatewayModels: string[];
  refreshingModels: boolean;
  onChange: (index: number, updater: (item: SubAgentItem) => SubAgentItem) => void;
  onRefreshModels: () => void;
  onDelete: (index: number) => void;
  onClose: () => void;
  isMobile?: boolean;
}) {
  const [activeTab, setActiveTab] = useState<SubAgentEditorTab>("basic");

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
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-gray-400 hover:bg-black/[0.04] hover:text-gray-600"
          >
            <X className="h-4 w-4" />
          </button>
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
                  className="form-input"
                  placeholder="image_analyzer"
                />
              </FormField>
              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <label className="text-[12px] font-medium text-gray-700">模型 *</label>
                  <button
                    type="button"
                    onClick={onRefreshModels}
                    disabled={refreshingModels}
                    className="flex items-center gap-1 text-[10px] text-[#002fa7] hover:text-[#001f7a] disabled:opacity-50"
                  >
                    <RefreshCw className={`h-3 w-3 ${refreshingModels ? "animate-spin" : ""}`} />
                    刷新路由模型
                  </button>
                </div>
                <select
                  value={item.model}
                  onChange={(e) => onChange(index, (it) => ({ ...it, model: e.target.value }))}
                  className="form-select"
                  disabled={gatewayModels.length === 0}
                >
                  {gatewayModels.length === 0 ? (
                    <option value="">未检测到 Higress 路由模型</option>
                  ) : (
                    gatewayModels.map((model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))
                  )}
                </select>
                <p className="mt-1 text-[10px] text-gray-400">
                  从 Higress 网关路由的模型中选择，点右上角「刷新路由模型」可同步最新配置。
                </p>
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
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  ariaLabel: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#002fa7]/40 ${
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

function RouteNode({
  title,
  detail,
  status,
  tone,
}: {
  title: string;
  detail: string;
  status: string;
  tone: "green" | "amber" | "blue";
}) {
  const tones = {
    green: "bg-emerald-50 text-emerald-700",
    amber: "bg-amber-50 text-amber-700",
    blue: "bg-[#002fa7]/8 text-[#002fa7]",
  };
  return (
    <div className="min-w-0 rounded-xl border border-black/[0.06] bg-white px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] font-semibold text-gray-800">{title}</span>
        <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium ${tones[tone]}`}>{status}</span>
      </div>
      <p className="mt-1.5 truncate text-[10px] text-gray-400" title={detail}>{detail}</p>
    </div>
  );
}

function ConnectionResult({ result }: { result: { ok: boolean; msg: string } }) {
  return (
    <div className={`mt-1.5 flex items-center gap-1 text-[11px] ${result.ok ? "text-emerald-600" : "text-red-500"}`}>
      {result.ok ? <ShieldCheck className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
      {result.msg}
    </div>
  );
}

function ToggleRow({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-black/[0.06] bg-white/60 px-3 py-2">
      <span className="min-w-0">
        <span className="block text-[11px] font-medium text-gray-600">{label}</span>
        {description ? <span className="mt-0.5 block text-[10px] leading-4 text-gray-400">{description}</span> : null}
      </span>
      <SwitchButton checked={checked} onChange={onChange} ariaLabel={label} />
    </div>
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
