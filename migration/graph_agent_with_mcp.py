"""AgentManager — Core Agent using LangChain create_agent API with DeepSeek.

基于 V5 结构，融合魔镜Claw 的优化：
- Token 预算感知 + Context Rot 检测
- AIMessage↔ToolMessage 配对保护
- 历史 tool_calls 正确还原
- SSE 事件增强（context_usage / new_response / error）
- MCP 持久 Session 模式（多服务器）
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

from langchain_core.messages import HumanMessage, AIMessage

from config import get_rag_mode, get_memory_backend, get_smart_extractor_config, load_config

# Claude Code 记忆类型标签映射
_MEM0_TYPE_LABELS: dict[str, str] = {
    "user": "用户偏好",
    "feedback": "行为规则",
    "project": "项目上下文",
    "reference": "参考信息",
}


def _format_mem0_context(typed_context: dict[str, list[str]]) -> str:
    sections: list[str] = []
    for mem_type in ("user", "feedback", "project", "reference"):
        items = typed_context.get(mem_type, [])
        if items:
            label = _MEM0_TYPE_LABELS[mem_type]
            bullet_list = "\n".join(f"- {item}" for item in items)
            sections.append(f"**{label}**\n{bullet_list}")
    return "\n\n".join(sections)


# ========== Context Rot 阈值 ==========
CONTEXT_ROT_WARNING_RATIO = 0.40
CONTEXT_ROT_CRITICAL_RATIO = 0.85


def _estimate_tokens(text) -> int:
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ascii_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + ascii_chars / 4)


from graph.prompt_builder import build_system_prompt
from graph.session_manager import session_manager, COMPRESSED_CONTEXT_PREFIX
from tools import get_all_tools


class AgentManager:
    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._tools: list = []
        self._llm = None
        self._config_sig: str = ""
        self._cached_agent = None
        self._cached_agent_key: str = ""
        self._mcp_client = None

    def initialize(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._tools = get_all_tools(base_dir)

        config = load_config()
        llm_config = config.get("llm", {})
        model = llm_config.get("model") or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        api_key = llm_config.get("api_key") or os.getenv("DEEPSEEK_API_KEY", "")
        api_base = llm_config.get("base_url") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        temperature = llm_config.get("temperature", 0.7)

        from langchain_deepseek import ChatDeepSeek
        self._llm = ChatDeepSeek(
            model=model,
            api_key=api_key,
            base_url=api_base,
            temperature=temperature,
            streaming=True,
        )
        self._config_sig = f"{model}|{api_key}|{api_base}|{temperature}"

        from graph.middlewares.skills_router import SkillsRouterMiddleware
        _router = SkillsRouterMiddleware()
        _tool_names = {t.name for t in self._tools}
        _missing = _router.validate_preferred_tools(_tool_names)
        if _missing:
            logger.warning("[agent] SkillsRouter preferred_tools not in loaded tools: %s", _missing)

        session_manager.initialize(base_dir)
        print(f"🤖 Agent initialized with {len(self._tools)} tools (model: {model})")

    def _refresh_llm_if_needed(self):
        config = load_config()
        llm_config = config.get("llm", {})
        model = llm_config.get("model") or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        api_key = llm_config.get("api_key") or os.getenv("DEEPSEEK_API_KEY", "")
        api_base = llm_config.get("base_url") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        temperature = llm_config.get("temperature", 0.7)

        config_sig = f"{model}|{api_key}|{api_base}|{temperature}"
        if self._config_sig != config_sig:
            from langchain_deepseek import ChatDeepSeek
            self._llm = ChatDeepSeek(
                model=model, api_key=api_key, base_url=api_base,
                temperature=temperature, streaming=True,
            )
            self._config_sig = config_sig

    def _get_prompt_files_sig(self) -> str:
        assert self._base_dir is not None
        files = [
            self._base_dir / "SKILLS_SNAPSHOT.md",
            self._base_dir / "workspace" / "SOUL.md",
            self._base_dir / "workspace" / "IDENTITY.md",
            self._base_dir / "workspace" / "USER.md",
            self._base_dir / "workspace" / "AGENTS.md",
            self._base_dir / "memory" / "MEMORY.md",
        ]
        mtimes = []
        for f in files:
            try:
                mtimes.append(str(f.stat().st_mtime))
            except FileNotFoundError:
                mtimes.append("0")
        return "|".join(mtimes)

    def _get_full_cache_key(self, rag_mode: bool, memory_backend: str, tool_reminder: bool) -> str:
        import json as _json
        from config import get_middleware_config, get_write_middleware_config, get_skills_router_config, get_cache_config
        config_sig = self._config_sig
        prompt_sig = self._get_prompt_files_sig()
        mw_sig = _json.dumps(get_middleware_config(), sort_keys=True)
        write_sig = _json.dumps(get_write_middleware_config(), sort_keys=True)
        skills_sig = _json.dumps(get_skills_router_config(), sort_keys=True)
        cache_sig = _json.dumps(get_cache_config(), sort_keys=True)
        return f"{config_sig}|{prompt_sig}|{rag_mode}|{memory_backend}|{tool_reminder}|{mw_sig}|{write_sig}|{skills_sig}|{cache_sig}"

    def _build_agent_core(self, tools: list, mem0_context: str = "", tool_reminder: bool = False):
        """构建 Agent 的纯逻辑，不涉及缓存。"""
        from langchain.agents import create_agent
        from graph.middlewares import (
            build_cache_middlewares,
            build_compression_middlewares,
            build_skills_router_middlewares,
            build_write_middlewares,
        )

        assert self._llm is not None
        memory_backend = get_memory_backend()
        rag_mode = get_rag_mode()

        system_prompt = build_system_prompt(
            self._base_dir,
            rag_mode=rag_mode,
            memory_backend=memory_backend,
            mem0_context=mem0_context,
            tool_reminder=tool_reminder,
        )

        cache_mws = build_cache_middlewares({
            "enabled": True,
            "cache_boundary": {"enabled": True},
            "tail_trim": {"enabled": True, "max_tokens": 50000, "head_keep": 2, "keep_recent": 30},
        })
        compression_mws = build_compression_middlewares(
            self._llm,
            {
                "enabled": True,
                "tool_clear": {"keep_recent": 50},
                "summarization": {"enabled": True, "trigger_tokens": 80000, "keep_messages": 10},
                "compaction": {"enabled": True, "trigger_tokens": 150000, "keep_recent": 4},
            },
        )
        # 将 ToolResultClear 提到 TailTrim 之前，否则 TailTrim 先删中段 ToolMessage，
        # 导致 ToolResultClear 永远看不到 >50 条 ToolMessage，永远触发不了。
        from graph.middlewares.compression import ToolResultClearMiddleware
        tool_clear_mw = None
        if compression_mws and isinstance(compression_mws[0], ToolResultClearMiddleware):
            tool_clear_mw = compression_mws.pop(0)
        skills_mws = build_skills_router_middlewares(get_skills_router_config())
        write_mws = build_write_middlewares(
            self._base_dir,
            {"enabled": True, "task_state": {"enabled": True, "todo_path": "workspace/TODO.md"}},
        )
        if tool_clear_mw:
            all_middlewares = [tool_clear_mw, *cache_mws, *compression_mws, *skills_mws, *write_mws]
        else:
            all_middlewares = [*cache_mws, *compression_mws, *skills_mws, *write_mws]

        return create_agent(
            model=self._llm,
            tools=tools,
            system_prompt=system_prompt,
            middleware=all_middlewares,
        )

    def _build_agent(self, mem0_context: str = "", tool_reminder: bool = False, extra_tools: list | None = None):
        """Build agent, using cache when possible.

        Args:
            extra_tools: 如果传入，不走缓存（用于 MCP 持久 session 模式）.
        """
        self._refresh_llm_if_needed()
        tools = list(self._tools)
        if extra_tools:
            tools.extend(extra_tools)

        if extra_tools:
            return self._build_agent_core(tools, mem0_context, tool_reminder)

        memory_backend = get_memory_backend()
        rag_mode = get_rag_mode()
        if memory_backend == "mem0":
            return self._build_agent_core(tools, mem0_context, tool_reminder)

        cache_key = self._get_full_cache_key(rag_mode, memory_backend, tool_reminder)
        if self._cached_agent is not None and self._cached_agent_key == cache_key:
            return self._cached_agent

        agent = self._build_agent_core(tools, mem0_context, tool_reminder)
        self._cached_agent = agent
        self._cached_agent_key = cache_key
        return agent

    # ========== MCP Client ==========
    async def _ensure_mcp_client(self) -> None:
        """异步创建 MCP Client（只执行一次）."""
        if self._mcp_client is not None:
            return

        cfg = load_config()
        mcp_cfg = cfg.get("mcp", {})
        enabled = mcp_cfg.get("enabled", [])
        if not enabled:
            return

        try:
            from mcp_clients import create_mcp_client
            self._mcp_client = create_mcp_client(enabled_names=enabled)
            logger.info("MCP client created, servers=%s", enabled)
        except Exception as e:
            logger.warning("Failed to create MCP client: %s", e)

    def _get_mcp_enabled(self) -> list[str]:
        """获取当前启用的 MCP 服务器列表."""
        cfg = load_config()
        mcp_cfg = cfg.get("mcp", {})
        return mcp_cfg.get("enabled", [])

    # ========== 核心升级：_build_messages ==========
    def _build_messages(self, user_message: str, history: list[dict[str, Any]]) -> list:
        from config import get_max_history_messages, get_context_window
        max_history = get_max_history_messages()
        context_window = get_context_window()
        warning_threshold = int(context_window * CONTEXT_ROT_WARNING_RATIO)
        critical_threshold = int(context_window * CONTEXT_ROT_CRITICAL_RATIO)

        truncated = list(history)
        if len(truncated) > max_history:
            first = truncated[0]
            if COMPRESSED_CONTEXT_PREFIX in first.get("content", ""):
                truncated = [first] + truncated[-(max_history - 1):]
            else:
                truncated = truncated[-max_history:]

        import ast
        messages = []
        for msg in truncated:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    def _parse_tool_args(inp):
                        if isinstance(inp, dict):
                            return dict(inp)
                        if isinstance(inp, str):
                            try:
                                return ast.literal_eval(inp)
                            except (ValueError, SyntaxError):
                                pass
                            try:
                                return json.loads(inp)
                            except Exception:
                                pass
                        return {}

                    lc_tool_calls = [
                        {"name": tc["tool"], "args": _parse_tool_args(tc.get("input", {})),
                         "id": tc.get("id") or f"tc_{i}"}
                        for i, tc in enumerate(tool_calls)
                    ]
                    messages.append(AIMessage(content=content, tool_calls=lc_tool_calls))
                    from langchain_core.messages import ToolMessage
                    for i, tc in enumerate(tool_calls):
                        output = tc.get("output", "")
                        if output:
                            tc_id = tc.get("id") or f"tc_{i}"
                            messages.append(ToolMessage(
                                content=str(output), tool_call_id=tc_id, name=tc["tool"]
                            ))
                else:
                    messages.append(AIMessage(content=content))

        messages.append(HumanMessage(content=user_message))

        total_tokens = sum(_estimate_tokens(m.content) for m in messages)
        if total_tokens > critical_threshold:
            keep_count = max(2, len(messages) // 2)
            start_idx = len(messages) - keep_count
            if start_idx < 0:
                start_idx = 0
            while start_idx > 0:
                msg = messages[start_idx]
                msg_type = getattr(msg, "type", "")
                if msg_type == "human":
                    break
                if msg_type == "ai" and not getattr(msg, "tool_calls", None):
                    break
                start_idx -= 1
            first_content = messages[0].content or ""
            if start_idx > 0 and COMPRESSED_CONTEXT_PREFIX in first_content:
                messages = [messages[0]] + messages[start_idx:]
            else:
                messages = messages[start_idx:]
            new_total = sum(_estimate_tokens(m.content) for m in messages)
            logger.warning(
                "Context exceeds critical threshold (%d > %d tokens), "
                "truncated to %d messages (%d tokens)",
                total_tokens, critical_threshold, len(messages), new_total,
            )
        elif total_tokens > warning_threshold:
            logger.warning(
                "Context approaching rot zone: %d tokens (threshold: %d).",
                total_tokens, warning_threshold,
            )

        return messages

    # ========== Tool Result 摘要 ==========
    TOOL_RESULT_CLEARING_THRESHOLD = 999999999

    async def _summarize_tool_result(self, content: str) -> str:
        if not self._llm:
            return content[:self.TOOL_RESULT_CLEARING_THRESHOLD] + "...[truncated]"
        try:
            resp = await self._llm.ainvoke([
                HumanMessage(content=f"用一句中文总结以下工具返回的关键发现（不超过80字）：\n{content[:2000]}")
            ])
            summary = resp.content.strip()
            return f"[工具结果摘要] {summary}"
        except Exception:
            return content[:self.TOOL_RESULT_CLEARING_THRESHOLD] + "...[truncated]"

    # ========== _run_agent_stream ==========
    async def _run_agent_stream(
        self, agent, messages: list, system_prompt_tokens: int
    ) -> AsyncGenerator[dict[str, Any], None]:
        full_response = ""
        tools_just_finished = False
        tool_outputs_tokens = 0

        try:
            async for event in agent.astream(
                {"messages": messages},
                stream_mode=["messages", "updates"],
            ):
                if isinstance(event, tuple):
                    mode, data = event
                else:
                    mode = "messages"
                    data = event

                if mode == "messages":
                    msg, metadata = data
                    if hasattr(msg, "content") and msg.content:
                        if msg.type == "AIMessageChunk" or msg.type == "ai":
                            if msg.content and not getattr(msg, "tool_calls", None):
                                if tools_just_finished:
                                    yield {"type": "new_response"}
                                    tools_just_finished = False
                                full_response += msg.content
                                yield {"type": "token", "content": msg.content}

                elif mode == "updates":
                    if isinstance(data, dict):
                        for node_name, node_data in data.items():
                            if node_name == "tools" and "messages" in node_data:
                                for tool_msg in node_data["messages"]:
                                    if hasattr(tool_msg, "name"):
                                        raw_output = str(tool_msg.content)
                                        if len(raw_output) > self.TOOL_RESULT_CLEARING_THRESHOLD:
                                            raw_output = await self._summarize_tool_result(raw_output)
                                            tool_msg.content = raw_output
                                        tc_id = getattr(tool_msg, "tool_call_id", "") or ""
                                        yield {
                                            "type": "tool_end",
                                            "tool": tool_msg.name,
                                            "output": str(tool_msg.content)[:2000],
                                            "id": tc_id,
                                        }
                                        tool_outputs_tokens += _estimate_tokens(str(tool_msg.content))
                                try:
                                    current_tokens = (
                                        sum(_estimate_tokens(m.content) for m in messages)
                                        + system_prompt_tokens
                                        + _estimate_tokens(full_response)
                                        + tool_outputs_tokens
                                    )
                                    ctx_window = get_context_window()
                                    yield {
                                        "type": "context_usage",
                                        "used_tokens": current_tokens,
                                        "total_tokens": ctx_window,
                                        "percentage": round(current_tokens / ctx_window * 100, 1),
                                    }
                                except Exception:
                                    pass
                                tools_just_finished = True
                            elif node_name == "model" and "messages" in node_data:
                                for agent_msg in node_data["messages"]:
                                    if tools_just_finished and getattr(agent_msg, "content", None):
                                        yield {"type": "new_response"}
                                        tools_just_finished = False
                                    if hasattr(agent_msg, "tool_calls") and agent_msg.tool_calls:
                                        for tc in agent_msg.tool_calls:
                                            yield {
                                                "type": "tool_start",
                                                "tool": tc["name"],
                                                "input": str(tc.get("args", ""))[:1000],
                                                "id": tc.get("id", ""),
                                            }
        except Exception as e:
            logger.error("astream exception: %s: %s", type(e).__name__, e)
            error_msg = f"{type(e).__name__}: {e}"
            full_response += f"\n\n[Error] Tool execution failed: {error_msg}"
            yield {"type": "error", "message": f"Tool execution failed: {error_msg}"}

        try:
            final_tokens = (
                sum(_estimate_tokens(m.content) for m in messages)
                + system_prompt_tokens
                + _estimate_tokens(full_response)
                + tool_outputs_tokens
            )
            ctx_window = get_context_window()
            yield {
                "type": "context_usage",
                "used_tokens": final_tokens,
                "total_tokens": ctx_window,
                "percentage": round(final_tokens / ctx_window * 100, 1),
            }
        except Exception:
            pass

        yield {"type": "done", "content": full_response}

    # ========== astream ==========
    async def astream(
        self, message: str, history: list[dict[str, Any]], user_id: str = "default_user"
    ) -> AsyncGenerator[dict[str, Any], None]:
        memory_backend = get_memory_backend()
        rag_mode = get_rag_mode()
        rag_context = ""
        mem0_context = ""

        if memory_backend == "mem0":
            from graph.mem0_manager import mem0_manager
            se_cfg = get_smart_extractor_config()
            import asyncio, functools
            loop = asyncio.get_running_loop()
            typed_context, raw_results = await loop.run_in_executor(
                None,
                functools.partial(
                    mem0_manager.get_typed_context,
                    message, user_id=user_id,
                    score_threshold=se_cfg["score_threshold"],
                    stale_days=se_cfg["stale_days"],
                ),
            )
            if typed_context:
                yield {
                    "type": "retrieval",
                    "query": message,
                    "results": [
                        {"text": r["memory"], "score": r.get("score", 0), "source": "mem0"}
                        for r in raw_results if r.get("memory")
                    ],
                }
                mem0_context = _format_mem0_context(typed_context)

        elif rag_mode and self._base_dir:
            from graph.memory_indexer import get_memory_indexer
            indexer = get_memory_indexer(self._base_dir)
            results = indexer.retrieve(message)
            if results:
                yield {
                    "type": "retrieval",
                    "query": message,
                    "results": results,
                }
                snippets = "\n\n".join(
                    f"[片段 {i+1}] (score: {r['score']})\n{r['text']}"
                    for i, r in enumerate(results)
                )
                rag_context = f"[记忆检索结果]\n{snippets}"

        augmented_history = list(history)
        if rag_context:
            augmented_history.append({"role": "assistant", "content": rag_context})
        messages = self._build_messages(message, augmented_history)

        system_prompt = build_system_prompt(
            self._base_dir,
            rag_mode=rag_mode,
            memory_backend=memory_backend,
            mem0_context=mem0_context,
            tool_reminder=len(history) >= 12,
        )
        system_prompt_tokens = _estimate_tokens(system_prompt)

        try:
            exact_tokens = sum(_estimate_tokens(m.content) for m in messages) + system_prompt_tokens
            ctx_window = get_context_window()
            yield {
                "type": "context_usage",
                "used_tokens": exact_tokens,
                "total_tokens": ctx_window,
                "percentage": round(exact_tokens / ctx_window * 100, 1),
            }
        except Exception:
            pass

        # ========== MCP 持久 Session 模式 ==========
        await self._ensure_mcp_client()
        if self._mcp_client:
            enabled = self._get_mcp_enabled()
            if enabled:
                try:
                    from contextlib import AsyncExitStack
                    from langchain_mcp_adapters.tools import load_mcp_tools

                    async with AsyncExitStack() as stack:
                        all_mcp_tools = []
                        for server_name in enabled:
                            session = await stack.enter_async_context(
                                self._mcp_client.session(server_name)
                            )
                            tools = await load_mcp_tools(session)
                            all_mcp_tools.extend(tools)

                        logger.info(
                            "Loaded %d MCP tools from %d servers via persistent session",
                            len(all_mcp_tools), len(enabled)
                        )

                        agent = self._build_agent_core(
                            tools=list(self._tools) + all_mcp_tools,
                            mem0_context=mem0_context,
                            tool_reminder=len(history) >= 12,
                        )
                        async for event in self._run_agent_stream(agent, messages, system_prompt_tokens):
                            yield event
                    return
                except Exception as e:
                    logger.warning("Persistent session failed, falling back: %s", e)

        # 原有非 MCP 逻辑
        agent = self._build_agent(
            mem0_context=mem0_context,
            tool_reminder=len(history) >= 12,
        )
        async for event in self._run_agent_stream(agent, messages, system_prompt_tokens):
            yield event

    async def ainvoke(self, message: str, session_id: str, user_id: str = "default_user") -> str:
        history = session_manager.load_session_for_agent(session_id)

        # MCP 持久 session 模式
        await self._ensure_mcp_client()
        if self._mcp_client:
            enabled = self._get_mcp_enabled()
            if enabled:
                try:
                    from contextlib import AsyncExitStack
                    from langchain_mcp_adapters.tools import load_mcp_tools

                    async with AsyncExitStack() as stack:
                        all_mcp_tools = []
                        for server_name in enabled:
                            session = await stack.enter_async_context(
                                self._mcp_client.session(server_name)
                            )
                            tools = await load_mcp_tools(session)
                            all_mcp_tools.extend(tools)

                        agent = self._build_agent_core(
                            tools=list(self._tools) + all_mcp_tools,
                            tool_reminder=len(history) >= 12,
                        )
                        messages = self._build_messages(message, history)
                        result = await agent.ainvoke({"messages": messages})
                        final_messages = result.get("messages", [])
                        for msg in reversed(final_messages):
                            if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                                response = msg.content
                                session_manager.save_message(session_id, "user", message)
                                session_manager.save_message(session_id, "assistant", response)
                                return response
                        return "No response generated."
                except Exception as e:
                    logger.warning("Persistent session failed in ainvoke, falling back: %s", e)

        agent = self._build_agent(tool_reminder=len(history) >= 12)
        messages = self._build_messages(message, history)
        result = await agent.ainvoke({"messages": messages})

        final_messages = result.get("messages", [])
        for msg in reversed(final_messages):
            if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                response = msg.content
                session_manager.save_message(session_id, "user", message)
                session_manager.save_message(session_id, "assistant", response)
                return response
        return "No response generated."


agent_manager = AgentManager()
