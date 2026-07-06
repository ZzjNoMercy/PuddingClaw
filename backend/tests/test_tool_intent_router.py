from __future__ import annotations

from graph.middlewares.tool_intent_router import ToolIntentRouterMiddleware


def test_vehicle_sales_metric_routes_to_pandas_table_tool() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("比亚迪周销量的环比是多少")

    assert decision["matched"] is True
    assert decision["intents"] == ["table_analysis"]
    assert decision["preferred_tools"] == ["pandas_knowledge_query"]


def test_news_intent_still_routes_to_web_search() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("蔚来最近有什么新闻")

    assert decision["matched"] is True
    assert "web_search" in decision["intents"]
    assert decision["preferred_tools"][0] == "tavily_search"


def test_table_metric_wins_over_generic_knowledge_words() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("从知识库里看比亚迪销量环比")

    assert decision["matched"] is True
    assert decision["intents"] == ["table_analysis"]
    assert decision["preferred_tools"] == ["pandas_knowledge_query"]


def test_data_analysis_wins_over_knowledge_rag() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("从知识库做一下比亚迪周销量数据分析")

    assert decision["matched"] is True
    assert decision["intents"] == ["table_analysis"]
    assert decision["preferred_tools"] == ["pandas_knowledge_query"]


def test_document_knowledge_request_still_routes_to_llamaindex() -> None:
    decision = ToolIntentRouterMiddleware()._classify_intent("从知识库总结 AI 架构白皮书")

    assert decision["matched"] is True
    assert decision["intents"] == ["knowledge_rag"]
    assert decision["preferred_tools"] == ["llamaindex_knowledge_query"]
