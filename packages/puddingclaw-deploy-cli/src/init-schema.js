import { PROFILES } from "./config.js";
import { CliError } from "./errors.js";

// This is an execution map for init. Actual settings and probes live in the
// discovery/configuration functions that perform them; duplicating every field
// here made the displayed plan drift away from runtime behavior.
const STEPS = Object.freeze([
  ["provider.agent", "Agent 模型与 Provider", null],
  ["provider.multimodal", "图片分析 SubAgent 多模态模型", null],
  ["database.shared", "核心数据库（PostgreSQL 优先 / SQLite 保底）", null],
  ["harness.context", "上下文工程", null],
  ["harness.prompt_cache", "Prompt 缓存", null],
  ["harness.completion", "Goal 与验收", null],
  ["harness.terminal", "终端执行", null],
  ["harness.runtime", "运行保护", null],
  ["harness.subagents", "SubAgent", null],
  ["knowledge.storage", "知识目录与本地搜索", "knowledge"],
  ["knowledge.index", "多模态索引", "knowledge"],
  ["knowledge.rag", "RAG", "knowledge"],
  ["knowledge.mineru", "MinerU 富文档解析", "knowledge"],
  ["knowledge.wiki", "LLM Wiki 与 gbrain", "knowledge"],
  ["analytics.vanna", "智能问数与 Vanna", "analytics"],
  ["analytics.database_qa", "智能问数结果与存储", "analytics"],
  ["headless.worker", "Headless Worker", "headless_worker"],
].map(([id, label, extension]) => Object.freeze({ id, label, extension })));

const DEPENDENCIES = Object.freeze({
  "harness.context": ["provider.agent"],
  "harness.completion": ["provider.agent"],
  "harness.subagents": ["provider.agent", "provider.multimodal"],
  "knowledge.storage": ["database.shared"],
  "knowledge.index": ["knowledge.storage", "database.shared"],
  "knowledge.rag": ["knowledge.index", "provider.agent"],
  "knowledge.mineru": ["knowledge.storage"],
  "knowledge.wiki": ["database.shared", "knowledge.rag"],
  "analytics.vanna": ["database.shared", "provider.agent"],
  "analytics.database_qa": ["analytics.vanna"],
  "headless.worker": ["provider.agent"],
});

export function buildInitPlan(profile) {
  if (!Object.hasOwn(PROFILES, profile)) {
    throw new CliError(`unknown profile: ${profile}`, { code: "argument_error" });
  }
  const extensions = PROFILES[profile];
  const steps = STEPS.map((step) => ({
    ...step,
    depends_on: DEPENDENCIES[step.id] || [],
    status: !step.extension || extensions[step.extension] ? "selected" : "disabled",
  }));
  return {
    schema_version: 1,
    profile,
    extensions,
    execution_order: steps.filter((step) => step.status === "selected").map((step) => step.id),
    branches: {
      database: ["postgresql_detect", "existing_or_native_or_docker", "sqlite_fallback"],
      knowledge: extensions.knowledge
        ? ["catalog_storage", "pgvector_if_required", "milvus_optional", "embedding_if_milvus", "mineru_optional"]
        : [],
      analytics: extensions.analytics
        ? ["catalog_database", "read_only_data_source", "vanna_models", "result_store"]
        : [],
    },
    steps,
  };
}
