"""Tests for structured Agent sources and final citation mappings."""

import asyncio
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_tool_result_round_trip_keeps_sources_separate():
    from graph.citations import encode_tool_result, parse_tool_result

    encoded = encode_tool_result("检索答案", [{
        "title": "架构文档",
        "uri": "/knowledge/architecture.pdf",
        "document_id": "doc-1",
        "chunk_id": "chunk-2",
        "page": 12,
        "quote": "结构化来源不应依赖工具输出预览。",
        "score": 0.91,
    }])

    answer, sources = parse_tool_result(encoded, "call-1")

    assert answer == "检索答案"
    assert len(sources) == 1
    assert sources[0]["source_id"].startswith("src_")
    assert sources[0]["tool_call_id"] == "call-1"
    assert sources[0]["page"] == 12


def test_source_ids_are_deterministic_and_deduplicated():
    from graph.citations import dedupe_sources, normalize_source

    source = {
        "title": "同一文档",
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "quote": "相同片段",
    }
    first = normalize_source(source)
    second = normalize_source(source)

    assert first["source_id"] == second["source_id"]
    assert len(dedupe_sources([first, second])) == 1


def test_finalize_citations_rejects_unknown_sources_and_reuses_index():
    from graph.citations import finalize_citations, normalize_source

    source = normalize_source({
        "title": "真实来源",
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "quote": "证据",
    })
    source_id = source["source_id"]
    content = f"第一处。[^{source_id}] 第二处。[^{source_id}] 伪造。[^src_unknown]"

    citations = finalize_citations(content, [source])

    assert len(citations) == 2
    assert {item["display_index"] for item in citations} == {1}
    assert {item["source_id"] for item in citations} == {source_id}


def test_session_message_persists_sources_and_citations(tmp_path):
    from graph.session_manager import SessionManager

    manager = SessionManager()
    manager.initialize(tmp_path)
    manager.create_session("citation-session")
    source = {"source_id": "src_one", "title": "来源", "source_type": "file"}
    citation = {
        "citation_id": "cite_one",
        "source_id": "src_one",
        "display_index": 1,
        "status": "verified",
    }

    manager.save_message(
        "citation-session",
        "assistant",
        "答案[^src_one]",
        sources=[source],
        citations=[citation],
    )
    saved = manager.load_session("citation-session")[0]

    assert saved["sources"] == [source]
    assert saved["citations"] == [citation]


def test_agent_tool_end_emits_sources_without_embedding_them_in_preview():
    from graph.agent import AgentManager
    from graph.citations import encode_tool_result
    from langchain_core.messages import AIMessage, ToolMessage

    encoded = encode_tool_result("简洁答案", [{
        "title": "检索文档",
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "quote": "证据片段",
    }])

    class FakeAgent:
        async def astream(self, *_args, **_kwargs):
            yield ("updates", {"model": {"messages": [AIMessage(
                content="",
                tool_calls=[{"name": "search_knowledge_base", "args": {"query": "q"}, "id": "call-1"}],
            )]}})
            yield ("updates", {"tools": {"messages": [ToolMessage(
                content=encoded,
                tool_call_id="call-1",
                name="search_knowledge_base",
            )]}})

    async def collect():
        manager = AgentManager()
        return [event async for event in manager._run_agent_stream(
            FakeAgent(), messages=[], system_prompt_tokens=0
        )]

    events = asyncio.run(collect())
    tool_end = next(event for event in events if event["type"] == "tool_end")

    assert tool_end["output"] == "简洁答案"
    assert tool_end["raw_output"] == encoded
    assert tool_end["sources"][0]["title"] == "检索文档"
    assert tool_end["sources"][0]["tool_call_id"] == "call-1"


def test_chat_stream_emits_and_persists_sources_and_citations(tmp_path, monkeypatch):
    import api.chat as chat_api
    from graph.citations import normalize_source

    chat_api.session_manager.initialize(tmp_path)
    chat_api.session_manager.create_session("stream-session")
    source = normalize_source({
        "title": "流式来源",
        "document_id": "doc-stream",
        "chunk_id": "chunk-stream",
        "quote": "流式证据",
        "tool_call_id": "call-stream",
    })
    original_tool_output = '{"items":[{"title":"流式来源","url":"https://example.com/source"}]}'

    async def fake_astream(*_args, **_kwargs):
        yield {"type": "tool_start", "tool": "search_knowledge_base", "input": "{}", "id": "call-stream"}
        yield {
            "type": "tool_end",
            "tool": "search_knowledge_base",
            "output": "工具答案",
            "output_preview": "工具答案",
            "raw_output": original_tool_output,
            "id": "call-stream",
            "sources": [source],
        }
        yield {"type": "new_response"}
        yield {"type": "token", "content": f"最终答案[^{source['source_id']}]"}
        yield {"type": "done", "content": "done"}

    async def no_title(_session_id):
        return None

    monkeypatch.setattr(chat_api.agent_manager, "astream", fake_astream)
    monkeypatch.setattr(chat_api, "_generate_title", no_title)

    async def collect():
        return [event async for event in chat_api.event_generator(
            "问题", "stream-session", "test-user"
        )]

    events = asyncio.run(collect())
    event_names = [event["event"] for event in events]
    history = chat_api.session_manager.load_session("stream-session")
    final_message = history[-1]

    assert "source_found" in event_names
    assert "citations_finalized" in event_names
    assert final_message["sources"][0]["source_id"] == source["source_id"]
    assert final_message["citations"][0]["source_id"] == source["source_id"]
    assert history[-2]["tool_calls"][0]["raw_output"] == original_tool_output


def test_historical_tool_message_readapts_raw_output_without_mutating_session():
    import json
    from graph.agent import AgentManager

    raw_output = json.dumps({
        "results": [{
            "title": "历史网页来源",
            "url": "https://example.com/history",
            "snippet": "历史工具结果仍在进入模型前重新适配。",
        }]
    }, ensure_ascii=False)
    history = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "tool": "web_search",
            "input": "{'query': 'history'}",
            "id": "history-call",
            "output": "旧的展示输出",
            "raw_output": raw_output,
        }],
    }]

    messages = AgentManager()._build_messages("继续", history)
    tool_message = next(message for message in messages if getattr(message, "type", "") == "tool")

    assert "历史网页来源" in tool_message.content
    assert "src_" in tool_message.content
    assert history[0]["tool_calls"][0]["raw_output"] == raw_output


def test_tool_result_adapter_handles_aihot_items_json():
    import json
    from graph.tool_result_adapter import tool_result_adapter

    output = json.dumps({
        "items": [{
            "id": "news-1",
            "title": "OpenAI 发布新模型",
            "url": "https://example.com/openai-model",
            "source": "OpenAI",
            "summary": "模型能力和上下文窗口得到提升。",
            "publishedAt": "2026-06-22T08:00:00Z",
            "category": "ai-models",
            "score": 0.95,
        }]
    }, ensure_ascii=False)

    adapted = tool_result_adapter.adapt(
        output,
        tool_name="terminal",
        tool_input="curl https://aihot.virxact.com/api/public/items?mode=selected",
        tool_call_id="aihot-call",
    )

    assert adapted.adapter == "common_json"
    assert adapted.sources[0]["title"] == "OpenAI 发布新模型"
    assert adapted.sources[0]["uri"] == "https://example.com/openai-model"
    assert adapted.sources[0]["source_type"] == "web"
    assert adapted.sources[0]["metadata"]["published_at"] == "2026-06-22T08:00:00Z"


def test_tool_result_adapter_handles_tavily_schema():
    import json
    from graph.tool_result_adapter import tool_result_adapter

    output = json.dumps({
        "query": "LangGraph citations",
        "results": [{
            "title": "LangGraph Documentation",
            "url": "https://docs.example.com/langgraph",
            "snippet": "Tool messages can carry structured metadata.",
        }],
    })

    adapted = tool_result_adapter.adapt(output, tool_name="tavily_search")

    assert adapted.adapter == "common_json"
    assert len(adapted.sources) == 1
    assert adapted.sources[0]["quote"] == "Tool messages can carry structured metadata."


def test_tavily_search_tool_returns_structured_sources(monkeypatch):
    from graph.citations import parse_tool_result
    from tools.tavily_search_tool import TavilySearchTool

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{
                "title": "蔚来最新消息",
                "url": "https://example.com/nio",
                "content": "蔚来发布近期业务进展。",
                "score": 0.9,
            }]}

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("tools.tavily_search_tool.requests.post", lambda *args, **kwargs: Response())

    output = TavilySearchTool()._run("蔚来最近有什么新闻")
    context, sources = parse_tool_result(output)

    assert "蔚来最新消息" in context
    assert sources[0]["uri"] == "https://example.com/nio"
    assert sources[0]["metadata"]["adapter"] == "tavily_search"


def test_tavily_search_retries_transient_connection_error(monkeypatch):
    import requests
    from graph.citations import parse_tool_result
    from tools.tavily_search_tool import TavilySearchTool

    attempts = 0

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{
                "title": "比亚迪新闻",
                "url": "https://example.com/byd",
                "content": "近期动态",
            }]}

    def post(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise requests.ConnectionError("temporary disconnect")
        return Response()

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr("tools.tavily_search_tool.requests.post", post)
    monkeypatch.setattr("tools.tavily_search_tool.time.sleep", lambda _seconds: None)

    _, sources = parse_tool_result(TavilySearchTool()._run("比亚迪最近新闻"))

    assert attempts == 2
    assert sources[0]["uri"] == "https://example.com/byd"


def test_news_intent_routes_to_tavily_search():
    from graph.middlewares.skills_router import SkillsRouterMiddleware

    decision = SkillsRouterMiddleware()._classify_intent("蔚来最近有什么新闻")

    assert decision["matched"] is True
    assert "knowledge" in decision["skills"]
    assert decision["preferred_tools"][0] == "tavily_search"


def test_tool_result_adapter_handles_markdown_links_and_dedupes_urls():
    from graph.tool_result_adapter import tool_result_adapter

    output = (
        "1. [第一条新闻](https://example.com/news)\n"
        "   新闻摘要\n"
        "2. **重复链接**\n   https://example.com/news\n"
        "3. **第二条新闻**\n   https://example.org/other\n"
    )

    adapted = tool_result_adapter.adapt(
        output,
        tool_name="terminal",
        tool_input="curl https://example.com/search",
    )

    assert adapted.adapter == "markdown_links"
    assert [source["uri"] for source in adapted.sources] == [
        "https://example.com/news",
        "https://example.org/other",
    ]


def test_fetch_url_uses_requested_page_as_single_source():
    from graph.tool_result_adapter import tool_result_adapter

    output = "# Example Article\n正文里还有 [其他链接](https://other.example/path)。"
    adapted = tool_result_adapter.adapt(
        output,
        tool_name="fetch_url",
        tool_input="{'url': 'https://example.com/article'}",
        tool_call_id="fetch-call",
    )

    assert adapted.adapter == "fetch_url"
    assert len(adapted.sources) == 1
    assert adapted.sources[0]["uri"] == "https://example.com/article"
    assert adapted.sources[0]["title"] == "Example Article"


def test_fetch_url_search_page_extracts_outbound_results():
    from graph.tool_result_adapter import tool_result_adapter

    output = """# [](/?FORM=Z9FD1)

[](https://news.example.com/article-1)
## [比亚迪发布新车型](https://news.example.com/article-1)
第一条新闻摘要。

## [比亚迪销量继续增长](https://finance.example.org/article-2)
第二条新闻摘要。

[News](/news/search?q=nav)
"""
    adapted = tool_result_adapter.adapt(
        output,
        tool_name="fetch_url",
        tool_input="{'url': 'https://www.bing.com/news/search?q=BYD'}",
        tool_call_id="fetch-search",
    )

    assert adapted.adapter == "fetch_url_search_results"
    assert [source["title"] for source in adapted.sources] == [
        "比亚迪发布新车型",
        "比亚迪销量继续增长",
    ]
    assert [source["uri"] for source in adapted.sources] == [
        "https://news.example.com/article-1",
        "https://finance.example.org/article-2",
    ]
    assert all(
        source["metadata"]["adapter"] == "fetch_url_search_result"
        for source in adapted.sources
    )


def test_fetch_url_rejects_blocked_and_mojibake_pages():
    from graph.tool_result_adapter import tool_result_adapter

    google = tool_result_adapter.adapt(
        "Please click here if you are not redirected within a few seconds. enablejs",
        tool_name="fetch_url",
        tool_input="{'url': 'https://www.google.com/search?q=BYD'}",
    )
    baidu = tool_result_adapter.adapt(
        "ç½ç»ä¸ç»åï¼è¯·ç¨åéè¯",
        tool_name="fetch_url",
        tool_input="{'url': 'https://news.baidu.com/ns?word=BYD'}",
    )

    assert google.adapter == "fetch_url_rejected"
    assert google.sources == []
    assert baidu.adapter == "fetch_url_rejected"
    assert baidu.sources == []


def test_read_file_skill_markdown_does_not_create_sources():
    from graph.tool_result_adapter import tool_result_adapter

    skill_md = """# AI HOT Skill

线上：https://aihot.virxact.com

- Base URL: https://aihot.virxact.com
- 完整 OpenAPI: https://aihot.virxact.com/openapi.yaml
```bash
curl https://aihot.virxact.com/api/public/items
```
"""
    adapted = tool_result_adapter.adapt(
        skill_md,
        tool_name="read_file",
        tool_input="{'file_path': 'skills/aihot/SKILL.md'}",
    )

    assert adapted.adapter == "plain_text"
    assert adapted.sources == []


def test_read_file_json_document_does_not_create_sources():
    import json
    from graph.tool_result_adapter import tool_result_adapter

    output = json.dumps({
        "items": [{"title": "文档里的示例", "url": "https://example.com/demo"}]
    })
    adapted = tool_result_adapter.adapt(
        output,
        tool_name="read_file",
        tool_input="{'file_path': 'example.json'}",
    )

    assert adapted.adapter == "plain_text"
    assert adapted.sources == []
