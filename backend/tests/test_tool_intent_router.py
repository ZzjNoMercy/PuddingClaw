from __future__ import annotations

from graph.middlewares.tool_intent_router import ToolIntentRouterMiddleware
from tools.database_knowledge_tool import DatabaseKnowledgeInput, DatabaseKnowledgeQueryTool


def test_vehicle_sales_metric_routes_to_pandas_table_tool() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("比亚迪周销量的环比是多少")

    assert decision["matched"] is True
    assert decision["intents"] == ["table_analysis"]
    assert decision["preferred_tools"][0] == "pandas_knowledge_query"
    assert "database_sql_generate" in decision["preferred_tools"]


def test_news_intent_still_routes_to_web_search() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("蔚来最近有什么新闻")

    assert decision["matched"] is True
    assert "web_search" in decision["intents"]
    assert decision["preferred_tools"][0] == "tavily_search"


def test_database_business_question_routes_directly_without_schema_probe() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("从数据库查询一下比亚迪及下属品牌6月上市的车型的价格")

    assert decision["matched"] is True
    assert decision["intents"] == ["database_analysis"]
    assert decision["preferred_tools"] == ["database_sql_generate", "database_sql_validate", "database_sql_execute"]
    assert "先把用户原问题交给 database_sql_generate" in decision["routing_prompt"]
    assert "需要元数据时使用 database_schema_inspect" in decision["routing_prompt"]


def test_database_tool_schema_discourages_metadata_probe_for_business_questions() -> None:
    tool = DatabaseKnowledgeQueryTool()
    question_field = DatabaseKnowledgeInput.model_fields["question"]

    assert "call it once with the user's original question" in tool.description
    assert "Do not make preliminary calls" in tool.description
    assert "pass the user's original question directly" in str(question_field.description)
    assert "do not first ask this tool to list tables" in str(question_field.description)


def test_table_metric_wins_over_generic_knowledge_words() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("从知识库里看比亚迪销量环比")

    assert decision["matched"] is True
    assert decision["intents"] == ["table_analysis"]
    assert decision["preferred_tools"][0] == "pandas_knowledge_query"
    assert "database_sql_generate" in decision["preferred_tools"]


def test_data_analysis_wins_over_knowledge_rag() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("从知识库做一下比亚迪周销量数据分析")

    assert decision["matched"] is True
    assert decision["intents"] == ["table_analysis"]
    assert decision["preferred_tools"][0] == "pandas_knowledge_query"
    assert "database_sql_generate" in decision["preferred_tools"]


def test_document_knowledge_request_still_routes_to_llamaindex() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("从知识库总结 AI 架构白皮书")

    assert decision["matched"] is True
    assert decision["intents"] == ["knowledge_rag"]
    assert decision["preferred_tools"] == ["llamaindex_knowledge_query"]
