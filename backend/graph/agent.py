"""AgentManager — Core Agent using LangChain create_agent API with DeepSeek.

基于 V5 结构，融合魔镜Claw 的优化：
- Token 预算感知 + Context Rot 检测
- AIMessage↔ToolMessage 配对保护
- 历史 tool_calls 正确还原
- SSE 事件增强（context_usage / new_response / error）
- MCP 持久 Session 模式（多服务器）
- Token 用量统计（每轮 LLM 调用捕获 usage_metadata）
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

from langchain_core.messages import HumanMessage, AIMessage, AIMessageChunk

from config import get_rag_mode, get_memory_backend, get_smart_extractor_config, load_config, get_compaction_trigger_tokens, get_middleware_config, get_cache_config, get_skills_router_config, get_write_middleware_config

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
HISTORICAL_TOOL_OUTPUT_PREFIX = (
    "【历史工具输出：仅在用户明确追问该结果时作为背景使用；"
    "禁止在当前回复中复述、续写或当作当前任务结果。】\n"
)
MISSING_TOOL_OUTPUT_PLACEHOLDER = (
    "[工具执行失败/无返回] 历史记录中存在 tool_call，但没有保存到对应工具输出；"
    "这通常来自流中断或工具服务异常。"
)


def _estimate_tokens(text) -> int:
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ascii_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + ascii_chars / 4)


def _legacy_tool_output_looks_error(output: str) -> bool:
    """兼容旧工具的字符串错误格式。

    正式协议优先使用 ToolMessage.status == "error"。这个函数只用于兼容
    当前本地工具中仍然以普通字符串返回错误的实现。
    """
    text = output.strip()
    lower = text.lower()
    error_markers = (
        "error:",
        "❌",
        "错误：",
        "错误:",
        "[error]",
        "timed out",
        "timeout",
        "exception",
        "traceback",
        "[deep_research error]",
        "执行失败",
        "failed:",
        "access denied",
        "file not found",
        "not a valid tool",
    )
    return any(marker in lower or marker in text for marker in error_markers)


def _tool_error_notice(tool_name: str, output: str, *, is_error: bool = False) -> str | None:
    """把工具层错误转成用户可见的简短说明。

    LangGraph 有时会把工具调用错误包装成 ToolMessage，而不是抛异常。
    如果不显式处理，前端只显示工具卡片里的 Error，assistant 回复会停在
    “我来查询...”这类前置语，用户会以为 agent 无响应。
    """
    text = output.strip()
    if not is_error and not _legacy_tool_output_looks_error(text):
        return None
    if "is not a valid tool" in text:
        if tool_name == "search_patents" or "search_patents" in text:
            return (
                "\n\n专利检索工具这一步没有加载成功。"
                "我会先基于已经拿到的结果继续整理；如果当前没有可用结果，"
                "需要稍后重试或检查 `zhihuiya_patents` MCP 配置。"
            )
        return f"\n\n工具 `{tool_name}` 当前没有加载成功，无法继续执行这个步骤。"
    return f"\n\n工具 `{tool_name}` 执行失败：{text}"


def _mcp_patent_unavailable_reply() -> str:
    return (
        "专利检索服务这次没有连接成功，所以我暂时没法继续调用 `search_patents` 扩展查询。"
        "如果本轮前面已经拿到部分专利结果，我会先基于那些结果整理；如果还没有结果，"
        "建议稍后重试，或检查 `zhihuiya_patents` MCP 配置。"
    )


from graph.prompt_builder import build_system_prompt
from graph.session_manager import session_manager, COMPRESSED_CONTEXT_PREFIX
from graph.citations import format_sources_for_model
from graph.tool_result_adapter import tool_result_adapter
from graph.llm_input_logger import current_session_id, current_user_id, log_llm_input
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
            stream_usage=True,  # ← 新增：启用 usage_metadata 返回
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
                temperature=temperature, streaming=True, stream_usage=True,
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

    def _build_agent_core(self, tools: list, mem0_context: str = "", rag_context: str = "", tool_reminder: bool = False):
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
            rag_context=rag_context,
            tool_reminder=tool_reminder,
        )

        # Context Engineering 推荐顺序：
        # cache_boundary(observer) → tail_trim → tool_clear → summarization → compaction → skills_router → write
        cache_mws = build_cache_middlewares(get_cache_config())
        compression_mws = build_compression_middlewares(self._llm, get_middleware_config())
        skills_mws = build_skills_router_middlewares(get_skills_router_config())
        write_mws = build_write_middlewares(
            self._base_dir,
            get_write_middleware_config(),
        )
        all_middlewares = [
            *cache_mws,
            *compression_mws,
            *skills_mws,
            *write_mws,
        ]

        return create_agent(
            model=self._llm,
            tools=tools,
            system_prompt=system_prompt,
            middleware=all_middlewares,
        )

    def _build_agent(self, mem0_context: str = "", rag_context: str = "", tool_reminder: bool = False, extra_tools: list | None = None):
        """Build agent, using cache when possible.

        Args:
            extra_tools: 如果传入，不走缓存（用于 MCP 持久 session 模式）.
        """
        self._refresh_llm_if_needed()
        tools = list(self._tools)
        if extra_tools:
            tools.extend(extra_tools)

        if extra_tools:
            return self._build_agent_core(tools, mem0_context, rag_context, tool_reminder)

        memory_backend = get_memory_backend()
        rag_mode = get_rag_mode()
        if memory_backend == "mem0":
            return self._build_agent_core(tools, mem0_context, rag_context, tool_reminder)

        cache_key = self._get_full_cache_key(rag_mode, memory_backend, tool_reminder)
        if self._cached_agent is not None and self._cached_agent_key == cache_key:
            return self._cached_agent

        agent = self._build_agent_core(tools, mem0_context, rag_context, tool_reminder)
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

    @staticmethod
    def _looks_like_mcp_required(message: str, history: list[dict[str, Any]]) -> bool:
        """判断本轮是否明显依赖 MCP 工具。

        MCP session 偶发失败时，普通聊天可以降级到本地工具；但专利类请求如果
        静默降级，模型容易继续调用历史中出现过的 search_patents，触发
        "not a valid tool"。这类请求应直接给出清晰错误。
        """
        needles = ("专利", "patent", "search_patents", "智慧芽", "zhihuiya")
        text = message.lower()
        if any(n in text for n in needles):
            return True
        # 中文里“申请了哪些/公开了哪些/授权了哪些”经常省略“专利”二字，
        # 但对车企、技术主题的这类问法通常仍然是在查专利库。
        patent_intent_terms = ("申请", "公开", "授权", "发明", "实用新型")
        patent_subject_terms = ("车企", "车辆", "汽车", "智能座舱", "座舱", "agent")
        if any(n in text for n in patent_intent_terms) and any(n in text for n in patent_subject_terms):
            return True
        for msg in history[-8:]:
            content = str(msg.get("content", "")).lower()
            if any(n in content for n in needles):
                return True
            for tc in msg.get("tool_calls", []) or []:
                if any(n in str(tc.get("tool", "")).lower() for n in needles):
                    return True
        return False

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

                    normalized_tool_calls = []
                    for i, tc in enumerate(tool_calls):
                        tool_name = tc.get("tool") or tc.get("name") or "unknown_tool"
                        tc_id = tc.get("id") or f"tc_{i}"
                        normalized_tool_calls.append((tc, tool_name, tc_id))

                    lc_tool_calls = [
                        {"name": tool_name, "args": _parse_tool_args(tc.get("input", tc.get("args", {}))),
                         "id": tc_id}
                        for tc, tool_name, tc_id in normalized_tool_calls
                    ]
                    messages.append(AIMessage(content=content, tool_calls=lc_tool_calls))
                    from langchain_core.messages import ToolMessage
                    for tc, tool_name, tc_id in normalized_tool_calls:
                        output = tc.get("output", "")
                        if output is None or str(output).strip() == "":
                            output = MISSING_TOOL_OUTPUT_PLACEHOLDER
                        messages.append(ToolMessage(
                            content=f"{HISTORICAL_TOOL_OUTPUT_PREFIX}{output}",
                            tool_call_id=tc_id,
                            name=tool_name,
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

    # ========== Tool Result 摘要（单条超长兜底） ==========
    SINGLE_TOOL_OVERFLOW_THRESHOLD = 20000  # tokens

    async def _summarize_tool_result(self, content: str, tool_name: str = "") -> str:
        """单条 tool output 超过 20K tokens 时立即按 tool 类型摘要。

        返回带 "[摘要] " 前缀的摘要文本，并在外层标记 summary_source="single_tool_overflow"。
        """
        from graph.middlewares.compression import _get_tool_summary_prompt

        if not self._llm:
            return content[:20000] + "...[truncated]"
        try:
            prompt = _get_tool_summary_prompt(tool_name).format(tool_output=content)
            resp = await self._llm.ainvoke([HumanMessage(content=prompt)])
            summary = resp.content.strip()
            return f"{self._summary_prefix()}{summary}"
        except Exception:
            return content[:20000] + "...[truncated]"

    @staticmethod
    def _summary_prefix() -> str:
        return "[摘要] "

    async def _answer_from_partial_tool_results(
        self,
        messages: list,
        successful_tool_results: list[dict[str, str]],
        error_msg: str,
    ) -> str:
        """Generate a final answer when later tool calls fail after partial success."""
        last_user = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                last_user = str(msg.content)
                break

        snippets = []
        for i, result in enumerate(successful_tool_results, 1):
            output = result["output"]
            if len(output) > 6000:
                output = output[:6000] + "\n...[已截断]"
            snippets.append(f"[成功工具结果 {i}] tool={result['tool']}\n{output}")

        prompt = (
            "你是一个工具型 Agent。上一轮任务中，部分工具调用已经成功返回，"
            "但后续工具调用失败。请基于已成功返回的工具结果给用户一个可用的阶段性回答。\n\n"
            "要求：\n"
            "1. 不要声称检索完整；开头简短说明后续检索失败，因此以下基于已返回结果。\n"
            "2. 只使用已成功返回的工具结果，不要编造专利、公司、编号或日期。\n"
            "3. 如果结果很少，就明确说样本有限。\n"
            "4. 回复中文，简洁、有条理。\n\n"
            f"用户问题：{last_user}\n\n"
            f"后续工具错误：{error_msg}\n\n"
            "已成功返回的工具结果：\n"
            + "\n\n".join(snippets)
        )

        chunks: list[str] = []
        if hasattr(self._llm, "astream"):
            async for chunk in self._llm.astream([HumanMessage(content=prompt)]):
                content = getattr(chunk, "content", "")
                if content:
                    chunks.append(str(content))
            return "".join(chunks)

        result = await self._llm.ainvoke([HumanMessage(content=prompt)])
        return str(getattr(result, "content", result))

    @staticmethod
    def _recent_successful_tool_results_from_history(
        history: list[dict[str, Any]],
        *,
        limit: int = 12,
    ) -> list[dict[str, str]]:
        """Collect recent successful persisted tool outputs for fallback answers."""
        results: list[dict[str, str]] = []
        for msg in reversed(history):
            for tc in reversed(msg.get("tool_calls", []) or []):
                output = str(tc.get("output", "") or "")
                if not output.strip():
                    continue
                if tc.get("is_error"):
                    continue
                if tc.get("summary_source") == "missing_tool_output":
                    continue
                results.append({
                    "tool": str(tc.get("tool") or tc.get("name") or "unknown_tool"),
                    "output": output,
                })
                if len(results) >= limit:
                    return list(reversed(results))
        return list(reversed(results))

    # ========== _run_agent_stream ==========
    async def _run_agent_stream(
        self, agent, messages: list, system_prompt_tokens: int,
        user_id: str = "default_user", session_id: str = "",
    ) -> AsyncGenerator[dict[str, Any], None]:
        session_token = current_session_id.set(session_id)
        user_token = current_user_id.set(user_id)
        full_response = ""
        tools_just_finished = False
        tool_outputs_tokens = 0
        round_num = 0
        stream_start_time = time.time()
        # Track tool_call ids already emitted as tool_start to avoid duplicates
        # when LangGraph replays model updates.
        _emitted_tool_starts: set[str] = set()
        _pending_tool_starts: dict[str, dict[str, str]] = {}
        _emitted_tool_error_notice = False
        successful_tool_results: list[dict[str, str]] = []

        compaction_trigger = get_compaction_trigger_tokens()

        try:
            async for event in agent.astream(
                {"messages": messages},
                stream_mode=["messages", "updates", "custom"],
            ):
                if isinstance(event, tuple):
                    mode, data = event
                else:
                    mode = "messages"
                    data = event

                if mode == "messages":
                    msg, metadata = data
                    if hasattr(msg, "content") and msg.content:
                        # LangGraph's messages stream should be consumed as token chunks.
                        # Full AIMessage objects and replayed chunks can appear as graph
                        # state/history events. Only chunks emitted by the active model node
                        # are user-visible reply tokens.
                        if (
                            isinstance(msg, AIMessageChunk)
                            and metadata.get("langgraph_node") == "model"
                        ):
                            if msg.content and not getattr(msg, "tool_calls", None):
                                if tools_just_finished:
                                    yield {"type": "new_response"}
                                    tools_just_finished = False
                                full_response += msg.content
                                yield {"type": "token", "content": msg.content}
                    # Token 统计：检查 usage_metadata（stream 最后一个 chunk）
                    usage = getattr(msg, "usage_metadata", None)
                    if usage:
                        input_tok = usage.get("input_tokens", 0) or 0
                        output_tok = usage.get("output_tokens", 0) or 0
                        if input_tok or output_tok:
                            round_num += 1
                            try:
                                from graph.token_usage_store import record_token_usage
                                record_token_usage(
                                    user_id=user_id,
                                    session_id=session_id,
                                    round_num=round_num,
                                    input_tokens=input_tok,
                                    output_tokens=output_tok,
                                    total_tokens=input_tok + output_tok,
                                    start_time=stream_start_time,
                                )
                            except Exception:
                                pass

                elif mode == "updates":
                    if isinstance(data, dict):
                        for node_name, node_data in data.items():
                            if node_name == "tools" and "messages" in node_data:
                                for tool_msg in node_data["messages"]:
                                    if hasattr(tool_msg, "name"):
                                        raw_tool_output = str(tool_msg.content)
                                        tc_id = getattr(tool_msg, "tool_call_id", "") or ""
                                        pending_tool = _pending_tool_starts.get(tc_id, {})
                                        adapted = tool_result_adapter.adapt(
                                            raw_tool_output,
                                            tool_name=str(tool_msg.name or ""),
                                            tool_input=pending_tool.get("input", ""),
                                            tool_call_id=tc_id,
                                        )
                                        raw_output = adapted.answer_context
                                        sources = adapted.sources
                                        tool_msg.content = format_sources_for_model(raw_output, sources)
                                        summary_source = None
                                        is_error = getattr(tool_msg, "status", "success") == "error"
                                        if _estimate_tokens(raw_output) > self.SINGLE_TOOL_OVERFLOW_THRESHOLD:
                                            yield {
                                                "type": "context_maintenance",
                                                "status": "start",
                                                "phase": "single_tool_overflow",
                                                "message": "正在提炼超长工具结果...",
                                            }
                                            try:
                                                raw_output = await self._summarize_tool_result(raw_output, tool_name=tool_msg.name)
                                                tool_msg.content = format_sources_for_model(raw_output, sources)
                                                summary_source = "single_tool_overflow"
                                            finally:
                                                yield {
                                                    "type": "context_maintenance",
                                                    "status": "done",
                                                    "phase": "single_tool_overflow",
                                                }
                                        if tc_id:
                                            _pending_tool_starts.pop(tc_id, None)
                                        is_error_final = is_error or _legacy_tool_output_looks_error(raw_output)
                                        yield {
                                            "type": "tool_end",
                                            "tool": tool_msg.name,
                                            "output": raw_output,
                                            "output_preview": raw_output[:2000],
                                            "id": tc_id,
                                            "summary_source": summary_source,
                                            "is_error": is_error_final,
                                            "sources": sources,
                                        }
                                        if not is_error_final and str(tool_msg.content).strip():
                                            successful_tool_results.append({
                                                "tool": str(tool_msg.name),
                                                "output": str(tool_msg.content),
                                            })
                                        notice = _tool_error_notice(
                                            tool_msg.name,
                                            raw_output,
                                            is_error=is_error,
                                        )
                                        if notice and not _emitted_tool_error_notice:
                                            _emitted_tool_error_notice = True
                                            if tools_just_finished:
                                                yield {"type": "new_response"}
                                                tools_just_finished = False
                                            full_response += notice
                                            yield {"type": "token", "content": notice}
                                        tool_outputs_tokens += _estimate_tokens(str(tool_msg.content))
                                try:
                                    current_tokens = (
                                        sum(_estimate_tokens(m.content) for m in messages)
                                        + system_prompt_tokens
                                        + _estimate_tokens(full_response)
                                        + tool_outputs_tokens
                                    )
                                    yield {
                                        "type": "context_usage",
                                        "used_tokens": current_tokens,
                                        "total_tokens": compaction_trigger,
                                        "percentage": round(current_tokens / compaction_trigger * 100, 1),
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
                                            tc_id = tc.get("id", "")
                                            # Deduplicate: the same model update can be replayed
                                            # multiple times by LangGraph.
                                            if tc_id and tc_id in _emitted_tool_starts:
                                                continue
                                            if tc_id:
                                                _emitted_tool_starts.add(tc_id)
                                                _pending_tool_starts[tc_id] = {
                                                    "tool": str(tc["name"]),
                                                    "input": str(tc.get("args", ""))[:1000],
                                                }
                                            yield {
                                                "type": "tool_start",
                                                "tool": tc["name"],
                                                "input": str(tc.get("args", ""))[:1000],
                                                "id": tc_id,
                                            }

                elif mode == "custom":
                    # 透传中间件自定义事件（tool_result_clear / compaction 等）
                    if isinstance(data, dict) and "type" in data:
                        yield data
        except Exception as e:
            logger.error("astream exception: %s: %s", type(e).__name__, e)
            error_msg = f"{type(e).__name__}: {e}"
            failed_pending_output = f"Tool execution failed before completion: {error_msg}"
            for tc_id, tc in list(_pending_tool_starts.items()):
                yield {
                    "type": "tool_end",
                    "tool": tc["tool"],
                    "output": failed_pending_output,
                    "output_preview": failed_pending_output[:2000],
                    "id": tc_id,
                    "summary_source": None,
                    "is_error": True,
                }
            _pending_tool_starts.clear()

            if successful_tool_results and self._llm is not None:
                yield {
                    "type": "context_maintenance",
                    "status": "start",
                    "phase": "partial_answer",
                    "message": "正在基于已返回结果整理回答...",
                }
                try:
                    fallback_text = await self._answer_from_partial_tool_results(
                        messages=messages,
                        successful_tool_results=successful_tool_results,
                        error_msg=error_msg,
                    )
                except Exception as fallback_error:
                    logger.exception("partial answer fallback failed")
                    fallback_text = (
                        "\n\n后续工具调用失败；已获取到部分工具结果，但自动整理回答也失败了。"
                        f"\n\n工具错误：{error_msg}"
                        f"\n整理错误：{type(fallback_error).__name__}: {fallback_error}"
                    )
                finally:
                    yield {
                        "type": "context_maintenance",
                        "status": "done",
                        "phase": "partial_answer",
                    }

                if fallback_text:
                    if tools_just_finished:
                        yield {"type": "new_response"}
                        tools_just_finished = False
                    if full_response and not fallback_text.startswith(("\n", "。", "，", "；")):
                        fallback_text = "\n\n" + fallback_text
                    full_response += fallback_text
                    yield {"type": "token", "content": fallback_text}
            else:
                full_response += f"\n\n[Error] Tool execution failed: {error_msg}"
                yield {"type": "error", "message": f"Tool execution failed: {error_msg}"}

        try:
            final_tokens = (
                sum(_estimate_tokens(m.content) for m in messages)
                + system_prompt_tokens
                + _estimate_tokens(full_response)
                + tool_outputs_tokens
            )
            yield {
                "type": "context_usage",
                "used_tokens": final_tokens,
                "total_tokens": compaction_trigger,
                "percentage": round(final_tokens / compaction_trigger * 100, 1),
            }
        except Exception:
            pass

        yield {"type": "done", "content": full_response}
        current_session_id.reset(session_token)
        current_user_id.reset(user_token)

    # ========== astream ==========
    async def astream(
        self, message: str, history: list[dict[str, Any]], user_id: str = "default_user", session_id: str = ""
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

        messages = self._build_messages(message, history)

        system_prompt = build_system_prompt(
            self._base_dir,
            rag_mode=rag_mode,
            memory_backend=memory_backend,
            mem0_context=mem0_context,
            rag_context=rag_context,
            tool_reminder=len(history) >= 12,
        )
        system_prompt_tokens = _estimate_tokens(system_prompt)
        try:
            log_llm_input(
                source="pre_agent",
                system_message=system_prompt,
                messages=messages,
                session_id=session_id,
                user_id=user_id,
                metadata={
                    "phase": "astream",
                    "history_count": len(history),
                    "rag_context_len": len(rag_context),
                    "mem0_context_len": len(mem0_context),
                    "system_prompt_tokens_estimate": system_prompt_tokens,
                },
            )
        except Exception as e:
            logger.warning("[llm-input-log] failed to log pre_agent payload: %s", e)

        try:
            exact_tokens = sum(_estimate_tokens(m.content) for m in messages) + system_prompt_tokens
            compaction_trigger = get_compaction_trigger_tokens()
            yield {
                "type": "context_usage",
                "used_tokens": exact_tokens,
                "total_tokens": compaction_trigger,
                "percentage": round(exact_tokens / compaction_trigger * 100, 1),
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
                            rag_context=rag_context,
                            tool_reminder=len(history) >= 12,
                        )
                        async for event in self._run_agent_stream(agent, messages, system_prompt_tokens, user_id, session_id):
                            yield event
                    return
                except Exception as e:
                    logger.exception("Persistent MCP session failed")
                    if self._looks_like_mcp_required(message, history):
                        recent_results = self._recent_successful_tool_results_from_history(history)
                        if recent_results and self._llm is not None:
                            yield {
                                "type": "context_maintenance",
                                "status": "start",
                                "phase": "partial_answer",
                                "message": "正在基于已返回结果整理回答...",
                            }
                            try:
                                reply = await self._answer_from_partial_tool_results(
                                    messages=messages,
                                    successful_tool_results=recent_results,
                                    error_msg=f"{type(e).__name__}: {e}",
                                )
                            except Exception as fallback_error:
                                logger.exception("outer partial answer fallback failed")
                                reply = (
                                    "后续专利工具连接失败；我已经拿到部分检索和著录结果，"
                                    "但自动整理阶段也失败了。请基于上方已返回的工具结果继续，"
                                    f"或稍后重试。整理错误：{type(fallback_error).__name__}: {fallback_error}"
                                )
                            finally:
                                yield {
                                    "type": "context_maintenance",
                                    "status": "done",
                                    "phase": "partial_answer",
                                }
                        else:
                            reply = _mcp_patent_unavailable_reply()
                        yield {
                            "type": "token",
                            "content": reply,
                        }
                        yield {"type": "done", "content": reply}
                        return
                    logger.warning("Persistent MCP session failed, falling back to local tools: %s", e)

        # 原有非 MCP 逻辑
        agent = self._build_agent(
            mem0_context=mem0_context,
            rag_context=rag_context,
            tool_reminder=len(history) >= 12,
        )
        async for event in self._run_agent_stream(agent, messages, system_prompt_tokens, user_id, session_id):
            yield event

    async def ainvoke(self, message: str, session_id: str, user_id: str = "default_user") -> str:
        history = session_manager.load_session_for_agent(session_id)
        rag_context, mem0_context, _ = await self._retrieve_memory_context(message, user_id)

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
                            mem0_context=mem0_context,
                            rag_context=rag_context,
                            tool_reminder=len(history) >= 12,
                        )
                        messages = self._build_messages(message, history)
                        try:
                            log_llm_input(
                                source="pre_agent",
                                system_message=build_system_prompt(
                                    self._base_dir,
                                    rag_mode=get_rag_mode(),
                                    memory_backend=get_memory_backend(),
                                    mem0_context=mem0_context,
                                    rag_context=rag_context,
                                    tool_reminder=len(history) >= 12,
                                ),
                                messages=messages,
                                session_id=session_id,
                                user_id=user_id,
                                metadata={"phase": "ainvoke_mcp", "history_count": len(history)},
                            )
                        except Exception as e:
                            logger.warning("[llm-input-log] failed to log ainvoke MCP payload: %s", e)
                        session_token = current_session_id.set(session_id)
                        user_token = current_user_id.set(user_id)
                        result = await agent.ainvoke({"messages": messages})
                        current_session_id.reset(session_token)
                        current_user_id.reset(user_token)
                        final_messages = result.get("messages", [])
                        for msg in reversed(final_messages):
                            if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                                response = msg.content
                                session_manager.save_message(session_id, "user", message)
                                session_manager.save_message(session_id, "assistant", response)
                                return response
                        return "No response generated."
                except Exception as e:
                    logger.exception("Persistent MCP session failed in ainvoke")
                    if self._looks_like_mcp_required(message, history):
                        return _mcp_patent_unavailable_reply()
                    logger.warning("Persistent MCP session failed in ainvoke, falling back to local tools: %s", e)

        agent = self._build_agent(
            mem0_context=mem0_context,
            rag_context=rag_context,
            tool_reminder=len(history) >= 12,
        )
        messages = self._build_messages(message, history)
        try:
            log_llm_input(
                source="pre_agent",
                system_message=build_system_prompt(
                    self._base_dir,
                    rag_mode=get_rag_mode(),
                    memory_backend=get_memory_backend(),
                    mem0_context=mem0_context,
                    rag_context=rag_context,
                    tool_reminder=len(history) >= 12,
                ),
                messages=messages,
                session_id=session_id,
                user_id=user_id,
                metadata={"phase": "ainvoke", "history_count": len(history)},
            )
        except Exception as e:
            logger.warning("[llm-input-log] failed to log ainvoke payload: %s", e)
        session_token = current_session_id.set(session_id)
        user_token = current_user_id.set(user_id)
        result = await agent.ainvoke({"messages": messages})
        current_session_id.reset(session_token)
        current_user_id.reset(user_token)

        final_messages = result.get("messages", [])
        for msg in reversed(final_messages):
            if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                response = msg.content
                session_manager.save_message(session_id, "user", message)
                session_manager.save_message(session_id, "assistant", response)
                return response
        return "No response generated."

    async def _retrieve_memory_context(self, message, user_id):
        """供 astream/ainvoke 使用，统一的记忆检索入口。"""
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
                mem0_context = _format_mem0_context(typed_context)
        elif rag_mode and self._base_dir:
            from graph.memory_indexer import get_memory_indexer
            indexer = get_memory_indexer(self._base_dir)
            results = indexer.retrieve(message)
            if results:
                snippets = "\n\n".join(
                    f"[片段 {i+1}] (score: {r['score']})\n{r['text']}"
                    for i, r in enumerate(results)
                )
                rag_context = f"[记忆检索结果]\n{snippets}"

        return rag_context, mem0_context, None


agent_manager = AgentManager()
