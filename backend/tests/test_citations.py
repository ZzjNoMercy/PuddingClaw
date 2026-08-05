"""Tests for structured Agent sources and final citation mappings."""

import asyncio
import json
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


def test_parse_tool_result_extracts_envelope_from_execute_skill_wrapper():
    from graph.citations import encode_tool_result, parse_tool_result

    encoded = encode_tool_result("AI HOT 返回 1 条动态", [{
        "source_id": "src_aihot_demo",
        "title": "GPT-5.5 Instant 新版发布",
        "uri": "https://example.com/gpt-55",
        "document_id": "https://example.com/gpt-55",
        "chunk_id": "aihot-item",
        "source_type": "web",
        "quote": "OpenAI 推送更新。",
    }])
    wrapped = (
        "技能：aihot\n"
        "描述：AI HOT 中文资讯查询 Skill\n\n"
        "执行结果：\n"
        f"[scripts/aihot_query.py] {encoded}\n"
    )

    answer, sources = parse_tool_result(wrapped, "call-aihot")

    assert answer == "AI HOT 返回 1 条动态"
    assert len(sources) == 1
    assert sources[0]["source_id"] == "src_aihot_demo"
    assert sources[0]["tool_call_id"] == "call-aihot"


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


def test_sanitize_citation_markdown_keeps_source_markers_but_removes_sql_footnotes():
    from graph.citations import sanitize_citation_markdown

    content = (
        "查询句柄 `sql-gen-demo` [^sql-gen-demo]，真实来源[^src_valid]。\n\n"
        "[^sql-gen-demo]: 这不是可引用来源\n"
        "[^src_valid]: 模型也不应自行输出来源脚注定义\n"
    )

    sanitized = sanitize_citation_markdown(content)

    assert "`sql-gen-demo`" in sanitized
    assert "[^sql-gen-demo]" not in sanitized
    assert "这不是可引用来源" not in sanitized
    assert "[^src_valid]" in sanitized
    assert "模型也不应自行输出来源脚注定义" not in sanitized


def test_resolve_message_citations_reuses_only_cited_session_sources():
    from graph.citations import normalize_source, resolve_message_citations

    current = normalize_source({
        "source_id": "src_current",
        "title": "本轮检索结果",
        "uri": "https://example.com/current",
    })
    reused = normalize_source({
        "source_id": "src_reused",
        "title": "历史引用来源",
        "uri": "https://example.com/reused",
    })
    unrelated = normalize_source({
        "source_id": "src_unrelated",
        "title": "未被引用的历史来源",
        "uri": "https://example.com/unrelated",
    })

    sources, citations = resolve_message_citations(
        "本轮信息。[^src_current] 历史信息。[^src_reused]",
        [current],
        [reused, unrelated],
    )

    assert [source["source_id"] for source in sources] == ["src_current", "src_reused"]
    assert [citation["source_id"] for citation in citations] == ["src_current", "src_reused"]


def test_materialize_markdown_citations_links_web_and_local_sources(tmp_path):
    from graph.citations import materialize_artifact_citations

    local_markdown = tmp_path / "知识 资料.md"
    local_markdown.write_text("# 来源", encoding="utf-8")
    sources = [
        {
            "source_id": "src_web",
            "title": "网页来源",
            "uri": "https://example.com/article",
            "source_type": "web",
        },
        {
            "source_id": "src_local",
            "title": "本地知识",
            "uri": "/knowledge/imported/source.md",
            "source_type": "knowledge_base",
            "chunk_id": "text-3",
            "metadata": {
                "file_path": str(local_markdown),
                "chunk_title": "关键章节",
            },
        },
    ]

    rendered, report = materialize_artifact_citations(
        "网页结论[^src_web]，本地结论[^src_local]。",
        sources,
        file_path="/workspace/report.md",
    )
    rerendered, second_report = materialize_artifact_citations(
        rendered,
        sources,
        file_path="/workspace/report.md",
    )

    assert "网页结论[^1]，本地结论[^2]" in rendered
    assert "[^1]: [网页来源](<https://example.com/article>)" in rendered
    assert f"[^2]: [本地知识](<{local_markdown.resolve().as_uri()}>)" in rendered
    assert "章节：关键章节；片段：text-3" in rendered
    assert "puddingclaw-citation" not in rendered
    assert "src_web" not in rendered
    assert "src_local" not in rendered
    assert rerendered == rendered
    assert report == {"materialized": 2, "unresolved_source_ids": []}
    assert second_report == {"materialized": 0, "unresolved_source_ids": []}


def test_materialize_markdown_citations_reuses_existing_definition_number():
    from graph.citations import materialize_artifact_citations

    source = {
        "source_id": "src_web",
        "title": "网页来源",
        "uri": "https://example.com/article",
        "source_type": "web",
    }
    content = (
        "已有结论[^7]。新增结论[^src_web]。\n\n"
        "[^7]: [网页来源](<https://example.com/article>)\n"
    )

    rendered, report = materialize_artifact_citations(
        content,
        [source],
        file_path="/workspace/report.md",
    )

    assert "已有结论[^7]。新增结论[^7]。" in rendered
    assert rendered.count("[^7]:") == 1
    assert "src_web" not in rendered
    assert report == {"materialized": 1, "unresolved_source_ids": []}


def test_materialize_html_citations_uses_numbered_anchors():
    from graph.citations import materialize_artifact_citations

    rendered, report = materialize_artifact_citations(
        "<html><body><p>结论[^src_web]，再次引用[^src_web]。</p></body></html>",
        [{
            "source_id": "src_web",
            "title": "网页来源",
            "uri": "https://example.com/article",
            "source_type": "web",
        }],
        file_path="/workspace/report.html",
    )

    assert rendered.count('href="#cite-source-src_web"') == 2
    assert '<li id="cite-source-src_web" value="1">' in rendered
    assert '<a href="https://example.com/article">网页来源</a>' in rendered
    assert rendered.index("citation-references") < rendered.index("</body>")
    assert report == {"materialized": 1, "unresolved_source_ids": []}
    rerendered, _ = materialize_artifact_citations(
        rendered,
        [{
            "source_id": "src_web",
            "title": "网页来源",
            "uri": "https://example.com/article",
            "source_type": "web",
        }],
        file_path="/workspace/report.html",
    )
    assert rerendered == rendered


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
            "permalink": "https://aihot.virxact.com/items/news-1",
            "source": "OpenAI",
            "summary": "模型能力和上下文窗口得到提升。",
            "publishedAt": "2026-06-22T08:00:00Z",
            "category": "ai-models",
            "score": 0.95,
        }]
    }, ensure_ascii=False)

    adapted = tool_result_adapter.adapt(
        output,
        tool_name="execute",
        tool_input="curl https://aihot.virxact.com/api/public/items?mode=selected",
        tool_call_id="aihot-call",
    )

    assert adapted.adapter == "common_json"
    assert adapted.sources[0]["title"] == "OpenAI 发布新模型"
    assert adapted.sources[0]["uri"] == "https://aihot.virxact.com/items/news-1"
    assert adapted.sources[0]["source_type"] == "web"
    assert adapted.sources[0]["metadata"]["published_at"] == "2026-06-22T08:00:00Z"


def test_plain_network_response_uses_requested_endpoint_as_source():
    from graph.tool_result_adapter import tool_result_adapter

    adapted = tool_result_adapter.adapt(
        "最新结果是模型能力更新。",
        tool_name="execute",
        tool_input=json.dumps(
            {
                "command": (
                    'UA="custom-skill/1.0 (+https://skill.example/about)"\n'
                    'curl -sS -H "User-Agent: $UA" '
                    '"https://api.example.com/public/latest"'
                )
            }
        ),
        tool_call_id="call-plain-network",
    )

    assert adapted.adapter == "network_request"
    assert [source["uri"] for source in adapted.sources] == [
        "https://api.example.com/public/latest"
    ]
    assert adapted.sources[0]["quote"] == "最新结果是模型能力更新。"


def test_execute_skill_stdout_links_are_generic_sources():
    from graph.tool_result_adapter import tool_result_adapter

    adapted = tool_result_adapter.adapt(
        "技能：demo\n\n执行结果：\n[最新公告](https://example.com/latest)",
        tool_name="execute_skill",
        tool_input='{"skill_name":"demo"}',
        tool_call_id="call-skill",
    )

    assert adapted.adapter == "markdown_links"
    assert adapted.sources[0]["uri"] == "https://example.com/latest"


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
    from graph.middlewares.tool_intent_router import ToolIntentRouterMiddleware

    decision = ToolIntentRouterMiddleware()._classify_intent("蔚来最近有什么新闻")

    assert decision["matched"] is True
    assert "web_search" in decision["intents"]
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


def test_format_sources_for_model_omits_evidence_quotes_when_historical():
    """Historical projections keep only source_id ↔ title continuity; the
    evidence quote must stay out of the model payload (recoverable via
    read_evidence) while in-run callers keep the full quote."""
    from graph.citations import format_sources_for_model

    sources = [{
        "source_id": "src_abc123",
        "title": "架构文档",
        "page": 12,
        "quote": "这段证据原文不应该出现在历史投影里。",
    }]

    full = format_sources_for_model("答案", sources)
    assert "证据：这段证据原文不应该出现在历史投影里。" in full
    assert "src_abc123: 架构文档，第 12 页" in full

    slim = format_sources_for_model("答案", sources, include_evidence=False)
    assert "src_abc123: 架构文档，第 12 页" in slim
    assert "这段证据原文不应该出现在历史投影里。" not in slim
    assert "read_evidence" in slim  # 省略是刻意的,且原文可恢复
    assert "[^source_id]" in slim  # 引用协议说明保留
