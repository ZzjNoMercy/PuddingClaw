import { PROFILES } from "./config.js";
import { CliError } from "./errors.js";

const STEPS = Object.freeze([
  {
    id: "provider.agent",
    label: "Agent 模型与 Provider",
    extension: null,
    fields: [
      "provider_registry.providers.*.enabled",
      "provider_registry.providers.*.endpoints.*.base_url",
      "provider_registry.providers.*.credentials.*",
      "provider_registry.bindings.agent",
    ],
    probes: ["provider.endpoint", "provider.agent_model"],
  },
  {
    id: "harness.context",
    label: "上下文工程",
    extension: null,
    fields: [
      "compression.deepagents.summarization.enabled",
      "compression.deepagents.summarization.model_id",
      "compression.deepagents.summarization.trigger_tokens",
      "compression.deepagents.summarization.keep_tokens",
      "compression.deepagents.tool_context.enabled",
      "compression.deepagents.tool_context.immediate_compaction_enabled",
      "compression.deepagents.tool_context.single_tool_trigger_tokens",
      "compression.deepagents.tool_context.background_min_result_tokens",
      "compression.deepagents.tool_context.retain_tool_context_tokens",
    ],
    probes: ["provider.summarization_model"],
  },
  {
    id: "harness.prompt_cache",
    label: "Prompt 缓存",
    extension: null,
    fields: [
      "harness.prompt_cache.trace_part_diagnostics",
      "harness.prompt_cache.ordered_system_sections",
      "harness.prompt_cache.tail_routing_message",
      "harness.prompt_cache.deterministic_session_projection",
      "harness.prompt_cache.stable_tool_schema",
    ],
    probes: [],
  },
  {
    id: "harness.completion",
    label: "Goal 与验收",
    extension: null,
    fields: [
      "harness.completion.rubric.enabled",
      "harness.completion.rubric.model",
      "harness.completion.rubric.max_iterations",
      "harness.completion.rubric.max_stagnant_repairs",
      "harness.completion.rubric.custom_rules_enabled",
      "harness.completion.rubric.custom_rules",
      "harness.goals.enabled",
      "harness.goals.max_rounds",
    ],
    probes: ["provider.rubric_model"],
  },
  {
    id: "harness.terminal",
    label: "终端执行",
    extension: null,
    fields: [
      "harness.terminal.execution_mode",
      "harness.terminal.default_timeout_seconds",
    ],
    probes: ["harness.execution_runner"],
  },
  {
    id: "harness.runtime",
    label: "运行保护",
    extension: null,
    fields: [
      "harness.model_call_limit.enabled",
      "harness.model_call_limit.run_limit",
      "harness.model_call_limit.thread_limit",
      "harness.model_call_limit.exit_behavior",
    ],
    probes: [],
  },
  {
    id: "harness.subagents",
    label: "SubAgent",
    extension: null,
    fields: [
      "subagents.items.*.enabled",
      "subagents.items.*.name",
      "subagents.items.*.model",
      "subagents.items.*.description",
      "subagents.items.*.route_trigger",
      "subagents.items.*.tools.mode",
      "subagents.items.*.skills.mode",
      "subagents.items.*.skills.paths",
      "subagents.items.*.system_prompt",
    ],
    probes: ["subagents.models", "subagents.skill_paths", "subagents.route_triggers"],
  },
  {
    id: "database.shared",
    label: "PostgreSQL",
    any_extension: ["knowledge", "analytics"],
    fields: [
      "database.mode",
      "database.host",
      "database.port",
      "database.database",
      "database.username",
      "database.password_ref",
    ],
    probes: ["database.connection", "database.pgvector"],
  },
  {
    id: "knowledge.storage",
    label: "知识目录与本地搜索",
    extension: "knowledge",
    fields: [
      "knowledge.root_dir",
      "knowledge.search.enabled",
      "knowledge.search.directories",
      "knowledge.search.sources",
      "knowledge.search.exclude",
    ],
    probes: ["knowledge.root", "knowledge.search_index"],
  },
  {
    id: "knowledge.rag",
    label: "RAG",
    extension: "knowledge",
    fields: [
      "rag.top_k",
      "rag.similarity_threshold",
      "rag.hybrid.enabled",
      "rag.hybrid.mode",
      "rag.hybrid.text_vector_weight",
      "rag.hybrid.image_vector_weight",
      "rag.hybrid.bm25_weight",
      "rag.hybrid.candidate_top_k",
      "rag.rerank.enabled",
      "rag.rerank.provider",
      "rag.rerank.model",
      "rag.rerank.top_n",
      "rag.rerank.candidate_top_k",
    ],
    probes: ["provider.embedding_models", "provider.rerank_model"],
  },
  {
    id: "knowledge.index",
    label: "多模态索引",
    extension: "knowledge",
    fields: [
      "knowledge.multimodal_index.enabled",
      "knowledge.multimodal_index.vector_store",
      "knowledge.multimodal_index.milvus_uri",
      "knowledge.multimodal_index.text_collection",
      "knowledge.multimodal_index.image_collection",
      "knowledge.multimodal_index.bm25_enabled",
      "knowledge.multimodal_index.embedding_batch_size",
    ],
    probes: ["milvus.connection", "milvus.collections"],
  },
  {
    id: "knowledge.mineru",
    label: "MinerU 富文档解析",
    extension: "knowledge",
    fields: [
      "knowledge.mineru.base_url",
      "knowledge.mineru.runtime_output_dir",
      "knowledge.mineru.keep_runtime_output",
      "knowledge.mineru.connect_timeout_seconds",
      "knowledge.mineru.read_timeout_seconds",
    ],
    probes: ["mineru.health", "mineru.output_directory"],
  },
  {
    id: "knowledge.wiki",
    label: "LLM Wiki 与 gbrain",
    extension: "knowledge",
    fields: [
      "knowledge.llm_wiki.compiler_agent.model_id",
      "knowledge.llm_wiki.retrieval.hybrid_enabled",
      "knowledge.llm_wiki.gbrain.embedding_model_id",
      "knowledge.llm_wiki.gbrain.think_model_id",
    ],
    probes: ["provider.wiki_models", "gbrain.runtime"],
  },
  {
    id: "analytics.vanna",
    label: "智能问数与 Vanna",
    extension: "analytics",
    fields: [
      "vanna.enabled",
      "vanna.default_database_source_id",
      "vanna.default_dialect",
      "vanna.query.entity_top_k_default",
      "vanna.query.entity_top_k_by_type",
      "provider_registry.bindings.vanna_llm",
      "provider_registry.bindings.vanna_embedding",
    ],
    probes: ["analytics.database_source", "analytics.read_only_query", "vanna.collections"],
  },
  {
    id: "analytics.database_qa",
    label: "智能问数结果与存储",
    extension: "analytics",
    fields: [
      "analytics.database_qa.full_rows_token_budget",
      "analytics.database_qa.preview_rows_token_budget",
      "analytics.database_qa.profile_token_budget",
      "analytics.database_qa.full_rows_hard_row_cap",
      "analytics.database_qa.full_rows_hard_column_cap",
      "analytics.database_qa.max_cell_chars_for_llm",
      "analytics.database_qa.result_materialization_row_cap",
      "analytics.database_qa.query_timeout_ms",
      "analytics.database_qa.sql_generation_timeout_ms",
      "analytics.database_qa.result_store_enabled",
      "analytics.database_qa.result_store_ttl_hours",
      "analytics.database_qa.default_page_size",
      "analytics.database_qa.max_page_size",
      "analytics.database_qa.export_enabled",
      "analytics.database_qa.profile_enabled",
    ],
    probes: ["analytics.result_store"],
  },
  {
    id: "headless.worker",
    label: "Headless Worker",
    extension: "headless_worker",
    fields: [
      "extensions.headless_worker.enabled",
      "headless.backend_url",
      "headless.credential_ref",
    ],
    probes: ["headless.backend", "headless.capabilities"],
  },
]);

const DEPENDENCIES = Object.freeze({
  "harness.context": ["provider.agent"],
  "harness.completion": ["provider.agent"],
  "harness.subagents": ["provider.agent"],
  "knowledge.storage": ["provider.agent"],
  "database.shared": ["knowledge.storage"],
  "knowledge.index": ["database.shared"],
  "knowledge.rag": ["knowledge.index", "provider.agent"],
  "knowledge.mineru": ["knowledge.storage"],
  "knowledge.wiki": ["database.shared", "knowledge.rag"],
  "analytics.vanna": ["database.shared", "provider.agent"],
  "analytics.database_qa": ["analytics.vanna"],
  "headless.worker": ["provider.agent"],
});

const EXECUTION_ORDER = Object.freeze([
  "provider.agent",
  "harness.context",
  "harness.prompt_cache",
  "harness.completion",
  "harness.terminal",
  "harness.runtime",
  "harness.subagents",
  "knowledge.storage",
  "database.shared",
  "knowledge.index",
  "knowledge.rag",
  "knowledge.mineru",
  "knowledge.wiki",
  "analytics.vanna",
  "analytics.database_qa",
  "headless.worker",
]);

function enabledFor(step, extensions) {
  if (step.extension) return Boolean(extensions[step.extension]);
  if (step.any_extension) return step.any_extension.some((name) => extensions[name]);
  return true;
}

export function buildInitPlan(profile) {
  if (!Object.hasOwn(PROFILES, profile)) {
    throw new CliError(`unknown profile: ${profile}`, { code: "argument_error" });
  }
  const extensions = PROFILES[profile];
  const order = new Map(EXECUTION_ORDER.map((id, index) => [id, index]));
  const steps = STEPS.map((step) => ({
    ...step,
    depends_on: DEPENDENCIES[step.id] || [],
    status: enabledFor(step, extensions) ? "selected" : "disabled",
  })).sort((left, right) => (order.get(left.id) ?? 999) - (order.get(right.id) ?? 999));
  const selected = steps.filter((step) => step.status === "selected");
  return {
    schema_version: 1,
    profile,
    extensions,
    execution_order: steps.filter((step) => step.status === "selected").map((step) => step.id),
    branches: {
      knowledge: extensions.knowledge
        ? ["catalog_storage", "postgresql_or_sqlite", "milvus_optional", "embedding_if_milvus", "mineru_optional"]
        : [],
      analytics: extensions.analytics
        ? ["catalog_database", "read_only_data_source", "vanna_models", "result_store"]
        : [],
    },
    field_count: selected.reduce((total, step) => total + step.fields.length, 0),
    probe_count: selected.reduce((total, step) => total + step.probes.length, 0),
    steps,
  };
}

export const INIT_SETTINGS_PATHS = Object.freeze(
  [...new Set(STEPS.flatMap((step) => step.fields))].sort(),
);
