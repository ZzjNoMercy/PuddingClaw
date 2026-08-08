"""ToolIntentRouterMiddleware — 模型调用前的工具意图软路由。

设计要点：
- 只做工具选择引导，不执行工具，也不替模型做最终决策
- 通过 before_model 在最后一条用户消息末尾追加临时路由提示
- 不修改真正的 system prompt，避免破坏 DeepSeek prefix cache
- 当前仅保留四类核心意图：数据库问数、表格问数、知识库 RAG、网页检索

与 compression middleware 的叠加顺序：
    compression（修改 messages，外层）→ tool_intent_router（注入路由，中层）→ write（after_model 副作用，内层）
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)


_ROUTER_HINT_MARKER = "[系统路由提示]"


_DEFAULT_INTENT_REGISTRY: dict[str, dict[str, Any]] = {
    "semantic_dimension_build": {
        "keywords": [
            "刷新车系维度", "刷新维度", "重建维度", "构建维度", "构建语义维度",
            "全量构建车系", "全量刷新车系", "刷新所有品牌车系", "刷新全部车系",
            "刷新全部车系维度", "全部车系维度", "crosswalk 刷新",
        ],
        "preferred_tools": ["enqueue_semantic_dimension_build", "get_semantic_dimension_build_job"],
        "tool_categories": ["table"],
        "routing_prompt": (
            "用户意图为耗时的语义维度构建或刷新。不要同步运行构建脚本、不要读取全量 Crosswalk。"
            "若用户明确要求刷新全部/所有车系维度（包括 /build-semantic-dimension），这是已注册的固定构建："
            "直接且只调用一次 enqueue_semantic_dimension_build，参数为 dimension_id='vehicle_series'、"
            "adapter='vehicle_series_full'、requested_scope={'brands':'all'}。不要在入队前调用 read_file、"
            "read_resource、ls/glob/find、database_schema_inspect、pandas_knowledge_query、"
            "llamaindex_knowledge_query 或 get_semantic_dimension_build_job，也不要先解释或询问确认。"
            "拿到 job_id 后简要说明任务已后台运行并结束本轮。"
            "用户后续询问状态或要求发布时，先调用 get_semantic_dimension_build_job；构建完成只表示 staging 已就绪，"
            "发布仍须用户在原对话明确确认，并按 build-semantic-dimension Skill 的受控发布步骤执行。"
        ),
    },
    "knowledge_catalog": {
        "keywords": [
            "有哪些文件", "有哪些表格文件", "可用的表格文件", "所有可用的表格文件",
            "导入了哪些", "导入的数据集", "文件清单", "目录清单", "资产清单",
            "知识库文件", "知识库中所有", "当前知识库中所有",
        ],
        "preferred_tools": [],
        "tool_categories": ["filesystem"],
        "routing_prompt": (
            "用户意图为知识库文件/资产目录查询。不要调用 pandas_knowledge_query 或 "
            "llamaindex_knowledge_query；应使用文件系统 ls/glob 查看 /knowledge 下的文件和目录。"
        ),
    },
    "database_analysis": {
        "keywords": [
            "数据库", "数据源", "postgres", "postgresql", "sql", "vanna", "nl2sql",
            "数据库表", "表结构", "ddl", "实体字典", "结构化数据库",
            "问数 agent", "智能问数",
            # 汽车产品配置分析默认落在业务数据库，不先走 Excel/Pandas 或 schema 探测。
            "产品配置", "汽车配置", "配置分析", "配置率", "搭载率", "配备率", "装配率",
            "空气悬架", "空气悬挂", "激光雷达", "充电倍率", "能源类型", "车型级别",
        ],
        "preferred_tools": ["database_evidence_search", "database_sql_validate", "database_sql_execute"],
        "tool_categories": ["table"],
        "routing_prompt": (
            "用户意图为结构化数据库问数。只要问题涉及已配置数据库源、数据库表、SQL、"
            "Vanna/NL2SQL、实体字典、DDL、表结构，或用户明确在问数据库里的业务数据，"
            "或涉及汽车产品配置分析、配置率/搭载率/配备率、空气悬架、激光雷达、充电倍率等指标，"
            "先调用 database_evidence_search 获取相关结构、实体、历史 SQL 和 EAV 原始值证据，"
            "由 Agent 结合用户问题和证据编写 SQL，再调用 database_sql_validate 和 "
            "database_sql_execute。相似 SQL 只是参考，不是权威；Agent 可按需使用 "
            "database_schema_inspect 补充探测。旧 SQL 生成与校验工具不对 Agent 暴露。"
            "Excel/CSV 文件仍使用 pandas_knowledge_query。"
        ),
    },
    "table_analysis": {
        "keywords": [
            "excel", "xlsx", "xls", "csv", "tsv", "表格", "电子表格", "数据表",
            "刚才导入", "导入的 excel", "导入的表", "统计", "筛选", "排序", "分组",
            "聚合", "透视", "top", "行数", "列名", "字段", "sheet",
            "数据分析", "分析数据", "数据统计", "数据汇总", "问数", "看数", "报表",
            "指标", "指标计算", "明细", "汇总",
            # 业务问数常见指标词：用户不一定会说“Excel/表格”或“数据库”，但这些问题通常应先走结构化工具。
            "销量", "周销量", "月销量", "环比", "同比", "占比", "配置率", "渗透率",
            "品牌", "车型", "车系", "款型", "价格段", "终端", "批发", "零售",
        ],
        "preferred_tools": [
            "pandas_knowledge_query",
            "database_evidence_search",
            "database_sql_validate",
            "database_sql_execute",
        ],
        "tool_categories": ["table"],
        "routing_prompt": (
            "用户意图为表格问数。只要问题涉及已导入 Excel/CSV/TSV、刚才导入的表格、字段/列名、"
            "行数、筛选、排序、分组、聚合、趋势、Top N、数据分析/问数/报表，"
            "或销量/环比/同比/占比/配置率等业务指标，应优先使用结构化问数工具。"
            "如果用户是在问当前知识库有哪些文件、有哪些表格文件、导入了哪些数据集、文件清单、目录清单或资产清单，"
            "不要调用 pandas_knowledge_query；应使用文件系统 ls/glob 查看 /knowledge。"
            "如果上下文是 Excel/CSV/TSV 或用户说“导入的表格/Excel”，调用 pandas_knowledge_query；"
            "如果上下文是数据库源/数据库表/SQL/Vanna，先调用 database_evidence_search，"
            "由 Agent 编写 SQL，再调用 database_sql_validate 和 database_sql_execute。"
            "不要先调用 llamaindex_knowledge_query、glob 或 grep 来查结构化数据。"
        ),
    },
    "knowledge_rag": {
        "keywords": [
            "知识", "知识库", "文档", "图文", "pdf", "markdown", "md",
            "白皮书", "报告", "图片", "架构图", "检索", "查找", "查询",
            "rag", "knowledge",
        ],
        "preferred_tools": ["llm_wiki_query"],
        "tool_categories": ["knowledge"],
        "routing_prompt": (
            "用户意图为本地知识查询。先读取 /skills/llm-wiki/SKILL.md，并按其 Query 协议调用 "
            "llm_wiki_context(operation='query') 和 llm_wiki_query。不要默认并行调用 "
            "llamaindex_knowledge_query；仅在 Wiki 无直接命中或覆盖不足、用户要求原始证据或具体 "
            "PDF/Markdown/图片/图表、可能涉及尚未编译的新资料，或明确要求全面检索时，才读取 "
            "/skills/knowledge-search/SKILL.md 并用 llamaindex_knowledge_query 补充。GBrain 只用于有价值的"
            "实体关系、图谱遍历或结构化筛选，不得替代 Markdown Wiki。不要用 pandas_knowledge_query "
            "处理 PDF/Markdown。"
        ),
    },
    "web_search": {
        "keywords": [
            "网页", "url", "http", "https", "链接", "新闻", "资讯", "最近有什么",
            "最新消息", "近况", "联网", "搜索一下", "web", "search", "find",
        ],
        "preferred_tools": ["tavily_search", "fetch_url"],
        "tool_categories": ["knowledge"],
        "routing_prompt": (
            "用户意图为网页或最新信息检索。公开网页、新闻和近期动态优先使用 tavily_search；"
            "已有明确 URL 时才使用 fetch_url 抓正文。"
        ),
    },
}


class ToolIntentRouterMiddleware(AgentMiddleware):
    """基于关键词的工具意图软路由中间件。

    工作原理：
    - 每次 LLM 调用前，读取最近几条用户消息
    - 用可控关键词表判断当前更像哪类工具意图
    - 命中时把一段内部路由提示追加到最后一条 HumanMessage
    - 未命中时不注入，让模型按完整 tool schema 自行选择
    """

    def __init__(
        self,
        intent_registry: dict[str, dict[str, Any]] | None = None,
        history_window: int = 2,
    ) -> None:
        super().__init__()
        self.intent_registry = (
            dict(intent_registry) if intent_registry is not None else dict(_DEFAULT_INTENT_REGISTRY)
        )
        self.history_window = history_window
        self._last_decision: dict[str, Any] = {}

    def _extract_context_text(self, messages: list) -> str:
        """提取最近 N 条用户消息文本（N=history_window+1），用于意图分类。"""
        user_texts: list[str] = []
        count = 0
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, list):
                    content = "".join(
                        block.get("text", "") if isinstance(block, dict) else str(block)
                        for block in content
                    )
                user_texts.append(str(content))
                count += 1
                if count > self.history_window:
                    break
        return " ".join(user_texts).lower()

    def validate_preferred_tools(self, available_tool_names: set[str]) -> list[str]:
        """校验 preferred_tools 名称在实际工具集中存在。"""
        missing: list[str] = []
        for intent_id, intent_def in self.intent_registry.items():
            for tool_name in intent_def.get("preferred_tools", []):
                if tool_name not in available_tool_names:
                    missing.append(f"{intent_id}.{tool_name}")
        return missing

    def _classify_intent(self, text: str) -> dict[str, Any]:
        """关键词匹配分类器，返回匹配意图、优先工具和路由提示。"""
        matched_intents: list[str] = []
        routing_prompts: list[str] = []
        preferred_tools: list[str] = []

        for intent_id, intent_def in self.intent_registry.items():
            keywords = intent_def.get("keywords", [])
            if any(kw.lower() in text for kw in keywords):
                matched_intents.append(intent_id)
                routing_prompts.append(intent_def["routing_prompt"])
                preferred_tools.extend(intent_def.get("preferred_tools", []))

        if not matched_intents:
            return {"matched": False, "intents": [], "preferred_tools": [], "routing_prompt": ""}

        # 资产目录查询优先于表格问数：避免“有哪些表格文件”被 pandas 当成单表分析。
        if "semantic_dimension_build" in matched_intents and len(matched_intents) > 1:
            matched_intents = ["semantic_dimension_build"]
            routing_prompts = [self.intent_registry["semantic_dimension_build"]["routing_prompt"]]
            preferred_tools = list(self.intent_registry["semantic_dimension_build"].get("preferred_tools", []))

        if "knowledge_catalog" in matched_intents and len(matched_intents) > 1:
            matched_intents = ["knowledge_catalog"]
            routing_prompts = [self.intent_registry["knowledge_catalog"]["routing_prompt"]]
            preferred_tools = list(self.intent_registry["knowledge_catalog"].get("preferred_tools", []))

        # 数据库问数优先级最高：避免结构化数据库问题被 Excel/PDF/Web 路由抢走。
        if "database_analysis" in matched_intents and len(matched_intents) > 1:
            matched_intents = ["database_analysis"]
            routing_prompts = [self.intent_registry["database_analysis"]["routing_prompt"]]
            preferred_tools = list(self.intent_registry["database_analysis"].get("preferred_tools", []))

        # 表格问数优先级高于 RAG/Web：避免 Excel/CSV 被文档检索抢走。
        if "table_analysis" in matched_intents and len(matched_intents) > 1:
            matched_intents = ["table_analysis"]
            routing_prompts = [self.intent_registry["table_analysis"]["routing_prompt"]]
            preferred_tools = list(self.intent_registry["table_analysis"].get("preferred_tools", []))

        # URL/网页/最新信息优先走 web；纯本地知识库再走 RAG。
        if "web_search" in matched_intents and "knowledge_rag" in matched_intents:
            matched_intents.remove("knowledge_rag")
            routing_prompts = [self.intent_registry[i]["routing_prompt"] for i in matched_intents]
            preferred_tools = []
            for intent_id in matched_intents:
                preferred_tools.extend(self.intent_registry[intent_id].get("preferred_tools", []))

        return {
            "matched": True,
            "intents": matched_intents,
            "preferred_tools": list(dict.fromkeys(preferred_tools)),
            "routing_prompt": "\n".join(routing_prompts),
        }

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        """模型调用前：分类意图并追加内部路由提示。"""
        messages = state.get("messages", [])
        if not messages:
            return None

        cleaned = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, str) and f"\n\n{_ROUTER_HINT_MARKER}" in content:
                    content = content.split(f"\n\n{_ROUTER_HINT_MARKER}")[0]
                    cleaned.append(HumanMessage(content=content))
                else:
                    cleaned.append(msg)
            else:
                cleaned.append(msg)
        had_old_routing = len(cleaned) != len(messages)

        context_text = self._extract_context_text(cleaned)
        if not context_text:
            self._last_decision = {"matched": False, "intents": [], "preferred_tools": []}
            return {"messages": cleaned} if had_old_routing else None

        decision = self._classify_intent(context_text)
        self._last_decision = decision

        if not decision["matched"]:
            logger.debug("[ToolIntentRouter] no intent matched, using full tools")
            return {"messages": cleaned} if had_old_routing else None

        last_human_idx = None
        for i in range(len(cleaned) - 1, -1, -1):
            if isinstance(cleaned[i], HumanMessage):
                last_human_idx = i
                break

        if last_human_idx is not None:
            original_content = cleaned[last_human_idx].content
            routing_hint = f"\n\n{_ROUTER_HINT_MARKER} {decision['routing_prompt']}"
            cleaned[last_human_idx] = HumanMessage(content=original_content + routing_hint)

        logger.info(
            "[ToolIntentRouter] matched intents=%s, preferred_tools=%s",
            decision["intents"],
            decision["preferred_tools"],
        )
        result: dict[str, Any] = {"messages": cleaned}
        if "database_analysis" in decision["intents"]:
            # Server-owned marker consumed by the legacy generator admission
            # guard.  A feature flag alone is not enough: standalone/API
            # callers may intentionally keep using the legacy contract.
            result["_database_agent_sql_path"] = True
        return result


def build_tool_intent_router_middlewares(config: dict) -> list:
    """构造工具意图路由中间件。"""
    if not config.get("enabled", True):
        return []

    # 新配置名叫 intents；保留旧 skills 字段兼容已经写入的 config.json。
    intent_registry = config.get("intents") or config.get("skills")
    history_window = config.get("history_window", 2)

    return [ToolIntentRouterMiddleware(
        intent_registry=intent_registry,
        history_window=history_window,
    )]
