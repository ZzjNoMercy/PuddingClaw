"use client";

import { useState, useEffect, useCallback } from "react";
import Navbar from "@/components/layout/Navbar";
import {
  Bot,
  Sparkles,
  Database,
  HardDrive,
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
} from "lucide-react";
import {
  getSettings,
  updateSettings,
  testConnection,
  getCapabilities,
  type SystemSettings,
  type Capabilities,
} from "@/lib/settingsApi";
import { useApp } from "@/lib/store";
import MemoryEditor from "@/components/settings/MemoryEditor";
import CapabilitiesStatus from "@/components/settings/CapabilitiesStatus";
import Link from "next/link";

type SettingsCategory = "ai" | "rag" | "memory" | "data" | "advanced" | "system";

const CATEGORIES: { key: SettingsCategory; label: string; icon: React.ElementType; color: string }[] = [
  { key: "ai", label: "AI 接入", icon: Network, color: "#002fa7" },
  { key: "rag", label: "RAG 设置", icon: Database, color: "#7c3aed" },
  { key: "memory", label: "记忆管理", icon: Brain, color: "#7c3aed" },
  { key: "data", label: "数据管理", icon: HardDrive, color: "#10b981" },
  { key: "advanced", label: "高级设置", icon: Sliders, color: "#6b7280" },
  { key: "system", label: "系统状态", icon: Activity, color: "#002fa7" },
];

const LLM_PROVIDERS = [
  { value: "deepseek", label: "DeepSeek", baseUrl: "https://api.deepseek.com" },
  { value: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1" },
  { value: "qwen", label: "Qwen / DashScope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  { value: "custom", label: "自定义", baseUrl: "" },
];

const SETTINGS_CATEGORY_KEY = "settings:activeCategory";

export default function SettingsPage() {
  const { ragMode, toggleRagMode } = useApp();
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

  // AI Gateway form state
  const [gatewayBaseUrl, setGatewayBaseUrl] = useState("");
  const [gatewayHealthPath, setGatewayHealthPath] = useState("/health");
  const [gatewayFallback, setGatewayFallback] = useState(true);
  const [gatewayEnvironmentOverride, setGatewayEnvironmentOverride] = useState(false);
  const [gatewayTesting, setGatewayTesting] = useState(false);
  const [gatewayTestResult, setGatewayTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
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
  const [embProvider, setEmbProvider] = useState("openai");
  const [embModel, setEmbModel] = useState("text-embedding-3-small");
  const [embBaseUrl, setEmbBaseUrl] = useState("https://api.openai.com/v1");
  const [embApiKey, setEmbApiKey] = useState("");
  const [embApiKeyMasked, setEmbApiKeyMasked] = useState("");
  const [showEmbKey, setShowEmbKey] = useState(false);
  const [embTesting, setEmbTesting] = useState(false);
  const [embTestResult, setEmbTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  // RAG form state
  const [ragTopK, setRagTopK] = useState(3);
  const [ragThreshold, setRagThreshold] = useState(0.7);

  // Compression
  const [compRatio, setCompRatio] = useState(0.5);

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
        // Populate LLM fields
        setLlmProvider(s.llm.provider);
        setLlmModel(s.llm.model);
        setLlmBaseUrl(s.llm.base_url);
        setLlmApiKeyMasked(s.llm.api_key_masked);
        setTemperature(s.llm.temperature);
        setMaxTokens(s.llm.max_tokens);
        // Populate Embedding fields
        setEmbProvider(s.embedding.provider);
        setEmbModel(s.embedding.model);
        setEmbBaseUrl(s.embedding.base_url);
        setEmbApiKeyMasked(s.embedding.api_key_masked);
        // Populate RAG fields
        setRagTopK(s.rag.top_k);
        setRagThreshold(s.rag.similarity_threshold);
        // Compression
        setCompRatio(s.compression.ratio);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const showToast = useCallback((type: "success" | "error", message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 3000);
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await updateSettings({
        ai_gateway: {
          base_url: gatewayBaseUrl,
          health_path: gatewayHealthPath,
          fallback_to_direct: gatewayFallback,
        },
        llm: {
          provider: llmProvider,
          model: llmModel,
          base_url: llmBaseUrl,
          ...(llmApiKey ? { api_key: llmApiKey } : {}),
          temperature,
          max_tokens: maxTokens,
        },
        embedding: {
          provider: embProvider,
          model: embModel,
          base_url: embBaseUrl,
          ...(embApiKey ? { api_key: embApiKey } : {}),
        },
        rag: {
          enabled: ragMode,
          top_k: ragTopK,
          similarity_threshold: ragThreshold,
        },
        compression: {
          ratio: compRatio,
        },
      });
      showToast("success", "设置已保存");
      // Clear raw keys after save
      setLlmApiKey("");
      setEmbApiKey("");
      // Reload to get fresh masked keys
      const fresh = await getSettings();
      setLlmApiKeyMasked(fresh.llm.api_key_masked);
      setEmbApiKeyMasked(fresh.embedding.api_key_masked);
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }, [gatewayBaseUrl, gatewayHealthPath, gatewayFallback, llmProvider, llmModel, llmBaseUrl, llmApiKey, temperature, maxTokens, embProvider, embModel, embBaseUrl, embApiKey, ragMode, ragTopK, ragThreshold, compRatio, showToast]);

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
    const key = llmApiKey || settings?.llm.api_key_masked || "";
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
    const key = embApiKey || settings?.embedding.api_key_masked || "";
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

  if (loading) {
    return (
      <div className="h-screen flex flex-col app-bg">
        <Navbar />
        <div className="flex-1 flex items-center justify-center">
          <Loader2 className="w-6 h-6 animate-spin text-gray-400" />
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col app-bg">
      <Navbar />
      <div className="flex-1 flex overflow-hidden">
        {/* Left: Category Navigation */}
        <div className="w-52 glass-panel border-r border-black/[0.06] shrink-0 p-3">
          <Link
            href="/"
            className="flex items-center gap-2.5 px-3 py-2.5 mb-3 text-[13px] font-medium text-gray-700 bg-black/[0.04] hover:bg-black/[0.08] rounded-xl transition-all group"
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
                      ? "bg-white/70 text-gray-800 font-medium shadow-sm"
                      : "text-gray-500 hover:bg-white/40"
                  }`}
                >
                  <Icon className="w-3.5 h-3.5" style={active ? { color: cat.color } : {}} />
                  {cat.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* Right: Settings Form */}
        <div className="flex-1 overflow-y-auto p-6">
          <div className={`${category === "ai" ? "max-w-4xl" : "max-w-2xl"} mx-auto space-y-6`}>
            {category === "ai" && (
              <>
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <h1 className="text-[22px] font-semibold tracking-tight text-gray-900">AI 接入</h1>
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

                <div className="grid grid-cols-[1fr_28px_1fr_28px_1fr] items-center rounded-2xl border border-black/[0.06] bg-gradient-to-br from-white/90 to-[#f5f7ff]/80 p-4 shadow-sm">
                  <RouteNode title="PuddingClaw" detail="ModelClient · 统一入口" status="运行中" tone="green" />
                  <Route className="mx-auto h-4 w-4 text-gray-300" />
                  <RouteNode
                    title="Higress Gateway"
                    detail={capabilities?.ai_gateway.available ? (gatewayBaseUrl || "http://higress:8080/v1") : "未探测到，失败时回退 Provider 直连"}
                    status={capabilities?.ai_gateway.available ? "已接入" : "未接入"}
                    tone={capabilities?.ai_gateway.available ? "green" : "amber"}
                  />
                  <Route className="mx-auto h-4 w-4 text-gray-300" />
                  <RouteNode title={llmProvider === "deepseek" ? "DeepSeek" : llmProvider} detail={llmModel} status="主模型" tone="blue" />
                </div>

                <div className="rounded-2xl border border-[#002fa7]/15 bg-gradient-to-br from-white/90 to-[#f4f7ff]/80 p-5 shadow-sm">
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
                </div>
              </>
            )}
            {/* LLM Settings */}
            {category === "ai" && (
              <SettingsCard title="LLM 模型配置" icon={Bot} color="#002fa7">
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
              </SettingsCard>
            )}

            {/* Embedding Settings */}
            {category === "ai" && (
              <SettingsCard title="Embedding 模型配置" icon={Sparkles} color="#f59e0b">
                <FormField label="Provider">
                  <select
                    value={embProvider}
                    onChange={(e) => {
                      setEmbProvider(e.target.value);
                      if (e.target.value === "openai") setEmbBaseUrl("https://api.openai.com/v1");
                      if (e.target.value === "dashscope") setEmbBaseUrl("https://dashscope.aliyuncs.com/compatible-mode/v1");
                    }}
                    className="form-select"
                  >
                    <option value="openai">OpenAI</option>
                    <option value="dashscope">DashScope</option>
                    <option value="custom">自定义</option>
                  </select>
                </FormField>
                <FormField label="Model">
                  <input
                    type="text"
                    value={embModel}
                    onChange={(e) => setEmbModel(e.target.value)}
                    className="form-input"
                    placeholder="text-embedding-3-small"
                  />
                </FormField>
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
              <SettingsCard title="RAG 检索设置" icon={Database} color="#7c3aed">
                <div className="flex items-center justify-between gap-4 rounded-lg border border-black/[0.06] bg-white/50 px-3.5 py-3">
                  <div className="min-w-0">
                    <p className="text-[12px] font-medium text-gray-700">启用 RAG 检索</p>
                    <p className="mt-0.5 text-[11px] text-gray-500">
                      {ragMode ? "对话将检索 Memory 向量库" : "对话将使用完整上下文，不执行向量检索"}
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={ragMode}
                    aria-label="启用 RAG 检索"
                    onClick={toggleRagMode}
                    className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#002fa7]/40 ${
                      ragMode ? "bg-[#002fa7]" : "bg-gray-300"
                    }`}
                  >
                    <span
                      className={`pointer-events-none mt-0.5 inline-block h-5 w-5 rounded-full bg-white shadow-sm transition-transform ${
                        ragMode ? "translate-x-[22px]" : "translate-x-0.5"
                      }`}
                    />
                  </button>
                </div>
                <FormField label={`Top-K: ${ragTopK}`}>
                  <input
                    type="range"
                    min="1"
                    max="10"
                    step="1"
                    value={ragTopK}
                    onChange={(e) => setRagTopK(parseInt(e.target.value))}
                    className="w-full accent-[#7c3aed]"
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
                    className="w-full accent-[#7c3aed]"
                  />
                  <div className="flex justify-between text-[10px] text-gray-400 mt-0.5">
                    <span>宽松 (0)</span>
                    <span>严格 (1)</span>
                  </div>
                </FormField>
              </SettingsCard>
            )}

            {/* Data Management */}
            {category === "data" && (
              <SettingsCard title="数据管理" icon={HardDrive} color="#10b981">
                <div className="space-y-3">
                  <p className="text-[12px] text-gray-500">管理会话数据和缓存</p>
                  <button className="w-full px-4 py-2.5 text-[12px] font-medium text-gray-600 bg-white/60 border border-black/[0.06] rounded-lg hover:bg-white hover:shadow-sm transition-all text-left">
                    导出所有会话 (JSON)
                  </button>
                  <button className="w-full px-4 py-2.5 text-[12px] font-medium text-red-500 bg-red-50/50 border border-red-100 rounded-lg hover:bg-red-50 transition-all text-left">
                    清除所有会话数据
                  </button>
                  <button className="w-full px-4 py-2.5 text-[12px] font-medium text-gray-600 bg-white/60 border border-black/[0.06] rounded-lg hover:bg-white hover:shadow-sm transition-all text-left">
                    重置所有设置为默认值
                  </button>
                </div>
              </SettingsCard>
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
                <CapabilitiesStatus refreshIntervalMs={30000} />
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
    <div className="bg-white/60 backdrop-blur-xl rounded-2xl border border-black/[0.06] p-5 shadow-sm">
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
