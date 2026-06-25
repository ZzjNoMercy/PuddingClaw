"""测试上下文工程（Context Engineering）优化项。

覆盖范围：
  - 配置阈值：DeepSeek V4 1M 上下文窗口 + 分级兜底阈值
  - TailTrimMiddleware：cache-friendly 中段裁剪、HumanMessage 边界保护
  - ToolResultClearMiddleware：轮次边界、min_summary_length、摘要前缀、summary_source、emit 事件
  - _summarize_tool_result：单条超长 tool output 摘要（20K tokens）
  - CompactionMiddleware：全局 reset、动态截断、保留 System + 最近 8 条、emit 事件
  - SessionManager：archive 合并、update_tool_call_output、context_usage_peak
  - Tokens API：优先返回 context_usage_peak
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ══════════════════════════════════════════════════════════════════════
# 配置阈值
# ══════════════════════════════════════════════════════════════════════

class TestContextEngineeringConfig:
    """Context Engineering 推荐阈值与配置。"""

    def test_default_context_window_is_1m(self):
        from config import _DEFAULT_CONFIG
        assert _DEFAULT_CONFIG["fallback_llm"]["context_window"] == 1000000

    def test_tail_trim_threshold_is_200k(self):
        from config import _DEFAULT_CONFIG
        assert _DEFAULT_CONFIG["cache"]["tail_trim"]["max_tokens"] == 200000
        assert _DEFAULT_CONFIG["cache"]["tail_trim"]["head_keep"] == 2
        assert _DEFAULT_CONFIG["cache"]["tail_trim"]["keep_recent"] == 30
        assert _DEFAULT_CONFIG["cache"]["middle_trim"]["max_tokens"] == 200000
        assert _DEFAULT_CONFIG["cache"]["middle_trim"]["head_keep"] == 2
        assert _DEFAULT_CONFIG["cache"]["middle_trim"]["keep_recent"] == 30

    def test_tool_clear_thresholds(self):
        from config import _DEFAULT_CONFIG
        tc = _DEFAULT_CONFIG["compression"]["middleware"]["tool_clear"]
        assert tc["keep_recent"] == 10
        assert tc["min_summary_length"] == 500

    def test_summarization_threshold(self):
        from config import _DEFAULT_CONFIG
        sm = _DEFAULT_CONFIG["compression"]["middleware"]["summarization"]
        assert sm["trigger_tokens"] == 200000
        assert sm["keep_messages"] == 10

    def test_compaction_threshold(self):
        from config import _DEFAULT_CONFIG
        cm = _DEFAULT_CONFIG["compression"]["middleware"]["compaction"]
        assert cm["trigger_tokens"] == 500000
        assert cm["keep_recent"] == 8
        assert cm["compact_budget_tokens"] == 120000

    def test_chat_preannounces_pending_tool_result_clear(self):
        from api.chat import _should_preannounce_tool_result_clear

        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"tool": "t", "output": "x" * 600}
                    for _ in range(11)
                ],
            }
        ]

        with patch("api.chat.get_middleware_config", return_value={
            "enabled": True,
            "tool_clear": {"keep_recent": 10, "min_summary_length": 500},
        }):
            assert _should_preannounce_tool_result_clear(history) is True

    def test_chat_does_not_preannounce_already_summarized_tool_results(self):
        from api.chat import _should_preannounce_tool_result_clear

        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "tool": "t",
                        "output": "[摘要] 已整理",
                        "summary_source": "tool_result_clear",
                    }
                    for _ in range(11)
                ],
            }
        ]

        with patch("api.chat.get_middleware_config", return_value={
            "enabled": True,
            "tool_clear": {"keep_recent": 10, "min_summary_length": 500},
        }):
            assert _should_preannounce_tool_result_clear(history) is False


# ══════════════════════════════════════════════════════════════════════
# Agent streaming
# ══════════════════════════════════════════════════════════════════════

class TestAgentStreaming:
    """流式回复只收集当前 token chunk，不保存图状态回放的旧 AIMessage。"""

    def test_ignores_full_ai_message_replay(self):
        from graph.agent import AgentManager
        from langchain_core.messages import AIMessage, AIMessageChunk

        class FakeAgent:
            async def astream(self, *_args, **_kwargs):
                yield ("messages", (AIMessage(content="上一轮 BYD 报告"), {}))
                yield (
                    "messages",
                    (AIMessageChunk(content="当前 SkillHub 回复"), {"langgraph_node": "model"}),
                )

        async def collect_events():
            manager = AgentManager()
            return [
                event
                async for event in manager._run_agent_stream(
                    FakeAgent(), messages=[], system_prompt_tokens=0
                )
            ]

        events = asyncio.run(collect_events())

        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        done = next(e for e in events if e["type"] == "done")

        assert token_text == "当前 SkillHub 回复"
        assert done["content"] == "当前 SkillHub 回复"
        assert "BYD" not in done["content"]

    def test_ignores_replayed_ai_message_chunk(self):
        from graph.agent import AgentManager
        from langchain_core.messages import AIMessageChunk

        class FakeAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    "messages",
                    (AIMessageChunk(content="搜索到15条专利，展示5条：比亚迪..."), {"langgraph_node": "history"}),
                )
                yield (
                    "messages",
                    (AIMessageChunk(content="好的，我来查询最近一周的 AI 论文信息。"), {"langgraph_node": "model"}),
                )

        async def collect_events():
            manager = AgentManager()
            return [
                event
                async for event in manager._run_agent_stream(
                    FakeAgent(), messages=[], system_prompt_tokens=0
                )
            ]

        events = asyncio.run(collect_events())

        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        done = next(e for e in events if e["type"] == "done")

        assert token_text == "好的，我来查询最近一周的 AI 论文信息。"
        assert done["content"] == "好的，我来查询最近一周的 AI 论文信息。"
        assert "比亚迪" not in done["content"]

    def test_historical_tool_outputs_are_labeled(self):
        from graph.agent import AgentManager, HISTORICAL_TOOL_OUTPUT_PREFIX

        manager = AgentManager()
        messages = manager._build_messages(
            "安装 SkillHub",
            [
                {
                    "role": "assistant",
                    "content": "上一轮分析",
                    "tool_calls": [
                        {
                            "tool": "search_patents",
                            "input": "{}",
                            "id": "tc1",
                            "output": "比亚迪旧专利结果",
                        }
                    ],
                }
            ],
        )

        tool_messages = [m for m in messages if getattr(m, "type", "") == "tool"]
        assert tool_messages
        assert tool_messages[0].content.startswith(HISTORICAL_TOOL_OUTPUT_PREFIX)
        assert "比亚迪旧专利结果" in tool_messages[0].content

    def test_build_messages_backfills_missing_tool_output(self):
        from graph.agent import AgentManager, MISSING_TOOL_OUTPUT_PLACEHOLDER

        manager = AgentManager()
        messages = manager._build_messages(
            "继续",
            [
                {
                    "role": "assistant",
                    "content": "我来查一下。",
                    "tool_calls": [
                        {
                            "tool": "search_patents",
                            "input": "{'query_text': '智能座舱'}",
                            "id": "tc_missing",
                        }
                    ],
                }
            ],
        )

        ai_messages = [m for m in messages if getattr(m, "type", "") == "ai"]
        tool_messages = [m for m in messages if getattr(m, "type", "") == "tool"]

        assert ai_messages[0].tool_calls[0]["id"] == "tc_missing"
        assert tool_messages[0].tool_call_id == "tc_missing"
        assert MISSING_TOOL_OUTPUT_PLACEHOLDER in tool_messages[0].content

    def test_chat_save_sanitizer_backfills_missing_tool_output(self):
        from api.chat import MISSING_TOOL_OUTPUT_PLACEHOLDER, _ensure_tool_call_outputs

        segment = {
            "content": "我来查一下。",
            "tool_calls": [
                {"tool": "search_patents", "id": "tc_missing", "input": "{}"}
            ],
        }

        normalized = _ensure_tool_call_outputs(segment)

        tc = normalized["tool_calls"][0]
        assert tc["output"] == MISSING_TOOL_OUTPUT_PLACEHOLDER
        assert tc["is_error"] is True
        assert tc["summary_source"] == "missing_tool_output"

    def test_chat_missing_tool_end_events_for_frontend(self):
        from api.chat import MISSING_TOOL_OUTPUT_PLACEHOLDER, _missing_tool_end_events

        segment = {
            "content": "我来查一下。",
            "tool_calls": [
                {"tool": "bibliography", "id": "tc_missing", "input": "{}"},
                {"tool": "bibliography", "id": "tc_done", "input": "{}", "output": "ok"},
            ],
        }

        events = _missing_tool_end_events(segment)

        assert len(events) == 1
        assert events[0]["tool"] == "bibliography"
        assert events[0]["id"] == "tc_missing"
        assert events[0]["output"] == MISSING_TOOL_OUTPUT_PLACEHOLDER
        assert events[0]["is_error"] is True
        assert events[0]["summary_source"] == "missing_tool_output"

    def test_tool_end_keeps_full_output_and_preview_separate(self):
        from graph.agent import AgentManager
        from langchain_core.messages import ToolMessage

        long_output = "x" * 2500

        class FakeAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    "updates",
                    {
                        "tools": {
                            "messages": [
                                ToolMessage(
                                    content=long_output,
                                    tool_call_id="tc1",
                                    name="terminal",
                                )
                            ]
                        }
                    },
                )

        async def collect_events():
            manager = AgentManager()
            return [
                event
                async for event in manager._run_agent_stream(
                    FakeAgent(), messages=[], system_prompt_tokens=0
                )
            ]

        events = asyncio.run(collect_events())
        tool_end = next(e for e in events if e["type"] == "tool_end")

        assert tool_end["output"] == long_output
        assert tool_end["output_preview"] == long_output[:2000]

    def test_tool_error_generates_user_visible_notice(self):
        from graph.agent import AgentManager
        from langchain_core.messages import ToolMessage

        tool_error = (
            "Error: search_patents is not a valid tool, try one of "
            "[terminal, read_file]."
        )

        class FakeAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    "updates",
                    {
                        "tools": {
                            "messages": [
                                ToolMessage(
                                    content=tool_error,
                                    tool_call_id="tc1",
                                    name="search_patents",
                                )
                            ]
                        }
                    },
                )

        async def collect_events():
            manager = AgentManager()
            return [
                event
                async for event in manager._run_agent_stream(
                    FakeAgent(), messages=[], system_prompt_tokens=0
                )
            ]

        events = asyncio.run(collect_events())
        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        done = next(e for e in events if e["type"] == "done")

        assert "专利检索工具这一步没有加载成功" in token_text
        assert "基于已经拿到的结果继续整理" in done["content"]
        assert "MCP 专利检索服务暂时不可用" not in done["content"]

    def test_generic_tool_error_generates_user_visible_notice(self):
        from graph.agent import AgentManager
        from langchain_core.messages import ToolMessage

        class FakeAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    "updates",
                    {
                        "tools": {
                            "messages": [
                                ToolMessage(
                                    content="❌ File not found: missing.txt",
                                    tool_call_id="tc1",
                                    name="read_file",
                                )
                            ]
                        }
                    },
                )

        async def collect_events():
            manager = AgentManager()
            return [
                event
                async for event in manager._run_agent_stream(
                    FakeAgent(), messages=[], system_prompt_tokens=0
                )
            ]

        events = asyncio.run(collect_events())
        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        done = next(e for e in events if e["type"] == "done")

        assert "工具 `read_file` 执行失败" in token_text
        assert "missing.txt" in done["content"]

    def test_tool_message_status_error_is_authoritative(self):
        from graph.agent import AgentManager
        from langchain_core.messages import ToolMessage

        class FakeAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    "updates",
                    {
                        "tools": {
                            "messages": [
                                ToolMessage(
                                    content="permission denied by harness",
                                    tool_call_id="tc1",
                                    name="terminal",
                                    status="error",
                                )
                            ]
                        }
                    },
                )

        async def collect_events():
            manager = AgentManager()
            return [
                event
                async for event in manager._run_agent_stream(
                    FakeAgent(), messages=[], system_prompt_tokens=0
                )
            ]

        events = asyncio.run(collect_events())
        tool_end = next(e for e in events if e["type"] == "tool_end")
        token_text = "".join(e["content"] for e in events if e["type"] == "token")

        assert tool_end["is_error"] is True
        assert "工具 `terminal` 执行失败" in token_text
        assert "permission denied by harness" in token_text

    def test_partial_tool_success_generates_answer_when_later_tool_fails(self):
        from graph.agent import AgentManager
        from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

        class FakeAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    "updates",
                    {
                        "tools": {
                            "messages": [
                                ToolMessage(
                                    content='{"success":true,"data":{"results":[{"pn":"CN1","title":"智能座舱Agent"}]}}',
                                    tool_call_id="tc1",
                                    name="search_patents",
                                )
                            ]
                        }
                    },
                )
                yield (
                    "updates",
                    {
                        "model": {
                            "messages": [
                                AIMessage(
                                    content="",
                                    tool_calls=[
                                        {
                                            "name": "search_patents",
                                            "args": {"query_text": "more"},
                                            "id": "tc2",
                                        }
                                    ],
                                )
                            ]
                        }
                    },
                )
                raise RuntimeError("MCP disconnected")

        class FakeLLM:
            async def astream(self, *_args, **_kwargs):
                yield AIMessageChunk(content="后续检索失败，先基于已返回结果回答：CN1 智能座舱Agent。")

        async def collect_events():
            manager = AgentManager()
            manager._llm = FakeLLM()
            return [
                event
                async for event in manager._run_agent_stream(
                    FakeAgent(),
                    messages=[HumanMessage(content="最近车企申请了哪些核心的智能座舱Agent")],
                    system_prompt_tokens=0,
                )
            ]

        events = asyncio.run(collect_events())
        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        failed_tool_end = [
            e for e in events
            if e["type"] == "tool_end" and e.get("id") == "tc2"
        ][0]

        assert not [e for e in events if e["type"] == "error"]
        assert failed_tool_end["is_error"] is True
        assert "后续检索失败" in token_text
        assert "CN1" in token_text

    def test_recent_successful_tool_results_skip_missing_output_placeholders(self):
        from graph.agent import AgentManager

        history = [
            {
                "role": "assistant",
                "content": "old",
                "tool_calls": [
                    {"tool": "search_patents", "output": "real search result"},
                    {
                        "tool": "bibliography",
                        "output": "[工具执行失败/无返回]",
                        "is_error": True,
                        "summary_source": "missing_tool_output",
                    },
                    {"tool": "bibliography", "output": "real bibliography"},
                ],
            }
        ]

        results = AgentManager._recent_successful_tool_results_from_history(history)

        assert results == [
            {"tool": "search_patents", "output": "real search result"},
            {"tool": "bibliography", "output": "real bibliography"},
        ]

    def test_single_tool_overflow_emits_context_maintenance_events(self):
        from graph.agent import AgentManager
        from langchain_core.messages import ToolMessage

        class FakeAgent:
            async def astream(self, *_args, **_kwargs):
                yield (
                    "updates",
                    {
                        "tools": {
                            "messages": [
                                ToolMessage(
                                    content="x" * 100,
                                    tool_call_id="tc1",
                                    name="terminal",
                                )
                            ]
                        }
                    },
                )

        async def collect_events():
            manager = AgentManager()
            manager.SINGLE_TOOL_OVERFLOW_THRESHOLD = 1
            return [
                event
                async for event in manager._run_agent_stream(
                    FakeAgent(), messages=[], system_prompt_tokens=0
                )
            ]

        events = asyncio.run(collect_events())
        maintenance = [
            (e.get("status"), e.get("phase"))
            for e in events
            if e["type"] == "context_maintenance"
        ]

        assert maintenance == [
            ("start", "single_tool_overflow"),
            ("done", "single_tool_overflow"),
        ]


# ══════════════════════════════════════════════════════════════════════
# TailTrimMiddleware
# ══════════════════════════════════════════════════════════════════════

class TestTailTrimMiddleware:
    """cache-friendly 中段裁剪。"""

    def test_does_not_trim_below_threshold(self):
        from langchain_core.messages import HumanMessage, AIMessage
        from graph.middlewares.cache import TailTrimMiddleware

        mw = TailTrimMiddleware(max_tokens=200000)
        messages = [
            HumanMessage(content="hi"),
            AIMessage(content="hello"),
            HumanMessage(content="q"),
        ]
        result = mw.before_model({"messages": messages}, None)
        assert result is None

    def test_tail_start_aligns_with_human_message(self):
        from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
        from graph.middlewares.cache import TailTrimMiddleware

        mw = TailTrimMiddleware(max_tokens=10, head_keep=1, keep_recent=2)
        # 使用足够长的内容确保 token 数 > max_tokens
        # ToolMessage 必须跟在带 tool_calls 的 AIMessage 后，才能被原子删除
        messages = [
            HumanMessage(content="start" * 100),
            AIMessage(content="a" * 100, tool_calls=[{"id": "tc1", "name": "x", "args": {}}], id="ai1"),
            ToolMessage(content="b" * 100, tool_call_id="tc1", id="tm1"),
            HumanMessage(content="keep me" * 100),
            AIMessage(content="c" * 100),
        ]
        result = mw.before_model({"messages": messages}, None)
        assert result is not None
        removed = result["messages"]
        assert len(removed) > 0


# ══════════════════════════════════════════════════════════════════════
# ToolResultClearMiddleware
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def fake_llm():
    llm = MagicMock()
    resp = MagicMock()
    resp.content = "摘要结果"
    llm.ainvoke = AsyncMock(return_value=resp)
    return llm


class TestToolResultClearMiddleware:
    """工具结果摘要：只处理最后一条 HumanMessage 之前的历史 tool。"""

    @pytest.mark.asyncio
    async def test_only_summarizes_before_last_human(self, fake_llm):
        from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
        from graph.middlewares.compression import ToolResultClearMiddleware, SUMMARY_PREFIX

        mw = ToolResultClearMiddleware(llm=fake_llm, keep_recent_tool_results=0, min_summary_length=10)
        messages = [
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            ToolMessage(content="x" * 1000, tool_call_id="tc1", name="read_file"),
            HumanMessage(content="q2"),
            AIMessage(content="a2"),
            ToolMessage(content="y" * 1000, tool_call_id="tc2", name="read_file"),
        ]
        runtime = MagicMock()
        runtime.stream_writer = MagicMock()
        result = await mw.abefore_model({"messages": messages}, runtime)
        assert result is not None
        new_msgs = result["messages"][1:]  # 去掉 RemoveMessage
        # 只有 tc1 被摘要（在最后一条 HumanMessage 之前）
        assert new_msgs[2].content.startswith(SUMMARY_PREFIX)
        assert new_msgs[2].tool_call_id == "tc1"
        # tc2 属于当前轮次，保持完整
        assert not new_msgs[5].content.startswith(SUMMARY_PREFIX)
        events = [call.args[0] for call in runtime.stream_writer.call_args_list]
        assert events == [
            {
                "type": "context_maintenance",
                "status": "start",
                "phase": "tool_result_clear",
                "message": "正在整理历史工具结果...",
            },
            {
                "type": "context_maintenance",
                "status": "done",
                "phase": "tool_result_clear",
            },
            {
                "type": "tool_result_clear",
                "tool_call_id": "tc1",
                "tool": "read_file",
                "summary": "摘要结果",
                "summary_source": "tool_result_clear",
            },
        ]

    @pytest.mark.asyncio
    async def test_short_output_not_summarized(self, fake_llm):
        from langchain_core.messages import HumanMessage, ToolMessage
        from graph.middlewares.compression import ToolResultClearMiddleware

        mw = ToolResultClearMiddleware(llm=fake_llm, keep_recent_tool_results=0, min_summary_length=500)
        messages = [
            HumanMessage(content="q"),
            ToolMessage(content="short" * 10, tool_call_id="tc1"),
        ]
        result = await mw.abefore_model({"messages": messages}, None)
        # 候选数量不足 + 长度不够，不触发
        assert result is None

    @pytest.mark.asyncio
    async def test_already_summarized_not_resummarized(self, fake_llm):
        from langchain_core.messages import HumanMessage, ToolMessage
        from graph.middlewares.compression import ToolResultClearMiddleware, SUMMARY_PREFIX

        mw = ToolResultClearMiddleware(llm=fake_llm, keep_recent_tool_results=0, min_summary_length=10)
        messages = [
            HumanMessage(content="q"),
            ToolMessage(content=f"{SUMMARY_PREFIX}已有摘要", tool_call_id="tc1"),
        ]
        result = await mw.abefore_model({"messages": messages}, None)
        # 已带前缀，不摘要
        assert result is None


# ══════════════════════════════════════════════════════════════════════
# _summarize_tool_result（单条超长兜底）
# ══════════════════════════════════════════════════════════════════════

class TestSingleToolOverflowSummary:
    """单条 tool output > 20K tokens 时立即摘要。"""

    @pytest.mark.asyncio
    async def test_threshold_is_20k_tokens(self):
        from graph.agent import AgentManager
        assert AgentManager.SINGLE_TOOL_OVERFLOW_THRESHOLD == 20000

    @pytest.mark.asyncio
    async def test_long_content_triggers_summary(self):
        from graph.agent import AgentManager
        from graph.middlewares.compression import SUMMARY_PREFIX

        mgr = AgentManager()
        mock_llm = AsyncMock()
        resp = MagicMock()
        resp.content = "专利摘要"
        mock_llm.ainvoke = AsyncMock(return_value=resp)
        mgr._llm = mock_llm

        result = await mgr._summarize_tool_result("x" * 50000, tool_name="patsnap_fetch")
        assert result.startswith(SUMMARY_PREFIX)
        assert "专利摘要" in result
        mock_llm.ainvoke.assert_called_once()


# ══════════════════════════════════════════════════════════════════════
# CompactionMiddleware
# ══════════════════════════════════════════════════════════════════════

class TestCompactionMiddleware:
    """全局 reset：保留 System + 最近 8 条，动态截断后生成摘要。"""

    @pytest.mark.asyncio
    async def test_compaction_keeps_system_and_recent(self, fake_llm):
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
        from graph.middlewares.compression import CompactionMiddleware, COMPRESSED_CONTEXT_PREFIX

        mw = CompactionMiddleware(model=fake_llm, trigger_tokens=10, keep_recent=2, compact_budget_tokens=1000)
        messages = [
            SystemMessage(content="sys"),
            HumanMessage(content="h1"),
            AIMessage(content="a1"),
            HumanMessage(content="h2"),
            AIMessage(content="a2"),
            HumanMessage(content="h3"),
        ]
        runtime = MagicMock()
        runtime.stream_writer = MagicMock()
        result = await mw.abefore_model({"messages": messages}, runtime)
        assert result is not None
        new_msgs = result["messages"][1:]  # 去掉 RemoveMessage
        assert isinstance(new_msgs[0], SystemMessage)
        assert COMPRESSED_CONTEXT_PREFIX in new_msgs[1].content
        assert len(new_msgs) == 4  # System + summary + 2 recent


# ══════════════════════════════════════════════════════════════════════
# SessionManager 持久化
# ══════════════════════════════════════════════════════════════════════

class TestSessionManagerPersistence:
    """session_manager 的持久化辅助函数。"""

    def test_update_tool_call_output(self, tmp_path):
        from graph.session_manager import SessionManager
        mgr = SessionManager()
        mgr.initialize(tmp_path)
        sid = "test-session"
        mgr.create_session(sid)
        mgr.save_message(
            sid,
            "assistant",
            "reply",
            tool_calls=[{"tool": "read_file", "id": "tc1", "output": "original content"}],
        )
        ok = mgr.update_tool_call_output(sid, "tc1", "[摘要] summarized", summary_source="tool_result_clear")
        assert ok
        data = mgr._read_file(sid)
        tc = data["messages"][0]["tool_calls"][0]
        assert tc["output"] == "[摘要] summarized"
        assert tc["summary_source"] == "tool_result_clear"

    def test_context_usage_peak(self, tmp_path):
        from graph.session_manager import SessionManager
        mgr = SessionManager()
        mgr.initialize(tmp_path)
        sid = "test-session"
        mgr.create_session(sid)
        mgr.update_context_usage_peak(sid, 1000)
        mgr.update_context_usage_peak(sid, 500)
        assert mgr.get_context_usage_peak(sid) == 1000

    def test_load_session_merges_archive(self, tmp_path):
        from graph.session_manager import SessionManager
        import json
        mgr = SessionManager()
        mgr.initialize(tmp_path)
        sid = "test-session"
        mgr.create_session(sid)
        mgr.save_message(sid, "user", "current")
        # 手动写入归档
        archive_dir = tmp_path / "sessions" / "archive"
        archive_dir.mkdir(exist_ok=True)
        archive_old = {
            "session_id": sid,
            "archived_at": 1,
            "messages": [{"role": "user", "content": "archived-old"}],
        }
        archive_new = {
            "session_id": sid,
            "archived_at": 2,
            "messages": [{"role": "user", "content": "archived-new"}],
        }
        (archive_dir / f"{sid}_1.json").write_text(
            json.dumps(archive_old), encoding="utf-8"
        )
        (archive_dir / f"{sid}_2.json").write_text(
            json.dumps(archive_new), encoding="utf-8"
        )
        messages = mgr.load_session(sid)
        assert [m["content"] for m in messages] == [
            "archived-old",
            "archived-new",
            "current",
        ]

    def test_load_session_for_agent_does_not_merge_final_answer_into_tool_call(self, tmp_path):
        from graph.session_manager import SessionManager

        mgr = SessionManager()
        mgr.initialize(tmp_path)
        sid = "test-session"
        mgr.create_session(sid)
        mgr.save_message(sid, "user", "search topic")
        mgr.save_message(
            sid,
            "assistant",
            "I will search now.",
            tool_calls=[{"tool": "search", "id": "tc1", "args": {"q": "topic"}}],
        )
        mgr.save_message(sid, "assistant", "Final answer from the searched topic.")

        agent_history = mgr.load_session_for_agent(sid)

        assert [m["role"] for m in agent_history] == ["user", "assistant", "assistant"]
        assert agent_history[1]["content"] == "I will search now."
        assert agent_history[1]["tool_calls"][0]["id"] == "tc1"
        assert agent_history[2]["content"] == "Final answer from the searched topic."
        assert "tool_calls" not in agent_history[2]

    def test_load_session_for_agent_still_merges_plain_consecutive_assistant_text(self, tmp_path):
        from graph.session_manager import SessionManager

        mgr = SessionManager()
        mgr.initialize(tmp_path)
        sid = "test-session"
        mgr.create_session(sid)
        mgr.save_message(sid, "assistant", "first")
        mgr.save_message(sid, "assistant", "second")

        agent_history = mgr.load_session_for_agent(sid)

        assert agent_history == [{"role": "assistant", "content": "first\nsecond"}]

    def test_middle_trim_archives_but_display_history_stays_complete(self, tmp_path):
        from graph.session_manager import MIDDLE_TRIM_CONTEXT_PREFIX, SessionManager

        mgr = SessionManager()
        mgr.initialize(tmp_path)
        sid = "test-session"
        mgr.create_session(sid)
        for content in ["head", "trim-user", "trim-assistant", "tail"]:
            role = "assistant" if "assistant" in content else "user"
            mgr.save_message(sid, role, content)

        archive_name = mgr.middle_trim_history(
            sid,
            "trimmed task was completed",
            1,
            3,
            metadata={"reason": "test"},
        )

        assert archive_name
        active = mgr.get_active_messages(sid)
        assert [m["content"] for m in active] == ["head", "tail"]

        display = mgr.load_session(sid)
        assert [m["content"] for m in display] == [
            "head",
            "trim-user",
            "trim-assistant",
            "tail",
        ]

        mgr.save_message(sid, "assistant", "future")
        assert [m["content"] for m in mgr.load_session(sid)] == [
            "head",
            "trim-user",
            "trim-assistant",
            "tail",
            "future",
        ]

        agent_history = mgr.load_session_for_agent(sid)
        assert agent_history[0]["content"].startswith(MIDDLE_TRIM_CONTEXT_PREFIX)
        assert "trimmed task was completed" in agent_history[0]["content"]
        assert all(m["content"] != "trim-user" for m in agent_history[1:])

    def test_middle_trim_span_aligns_tail_to_user(self):
        from api.chat import _select_middle_trim_span

        messages = [
            {"role": "user", "content": "head user"},
            {"role": "assistant", "content": "head assistant"},
            {"role": "user", "content": "middle user"},
            {"role": "assistant", "content": "middle assistant"},
            {"role": "tool", "content": "middle tool"},
            {"role": "user", "content": "tail user"},
            {"role": "assistant", "content": "tail assistant"},
        ]

        span = _select_middle_trim_span(
            messages,
            {"enabled": True, "max_tokens": 1, "head_keep": 2, "keep_recent": 2},
        )

        assert span == (2, 5)


# ══════════════════════════════════════════════════════════════════════
# Tokens API
# ══════════════════════════════════════════════════════════════════════

class TestTokensAPI:
    """/api/tokens/session/{id} 优先返回峰值。"""

    @pytest.mark.asyncio
    async def test_peak_takes_precedence(self, tmp_path):
        from api.tokens import get_session_token_count
        from graph.session_manager import session_manager
        from config import CONFIG_FILE

        with patch("config.CONFIG_FILE", tmp_path / "nonexistent_config.json"):
            session_manager.initialize(tmp_path)
            sid = "tokens-test"
            session_manager.create_session(sid)
            session_manager.save_message(sid, "user", "hello")
            session_manager.update_context_usage_peak(sid, 999999)

            with patch("api.tokens.build_system_prompt", return_value="sys"):
                with patch("api.tokens._count_tokens", return_value=1):
                    result = await get_session_token_count(sid)

        assert result["message_tokens"] == 999998
        assert result["total_tokens"] == 999999
        assert result["compaction_trigger"] == 500000
