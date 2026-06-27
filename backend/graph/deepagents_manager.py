"""DeepAgents runtime manager for Agent mode."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.middleware.memory import MemoryMiddleware
from langchain_core.messages import AIMessageChunk

from graph.citations import dedupe_sources, finalize_citations, format_sources_for_model
from graph.session_manager import session_manager
from graph.tool_result_adapter import tool_result_adapter
from llm.model_client import ModelClientChatModel
from projects.registry import project_registry
from tools import get_all_tools
import config

logger = logging.getLogger(__name__)

AGENT_MODE_PUDDINGCLAW_TOOLS = {
    "terminal",
    "fetch_url",
    "tavily_search",
    "search_knowledge_base",
}


async def _generate_title(session_id: str) -> str | None:
    """Generate a short title for Agent-mode sessions using the same title role."""

    try:
        messages = session_manager.load_session_for_agent(session_id)
        first_user = ""
        first_assistant = ""
        for msg in messages:
            if msg.get("role") == "user" and not first_user:
                first_user = str(msg.get("content") or "")[:200]
            elif msg.get("role") == "assistant" and not first_assistant:
                first_assistant = str(msg.get("content") or "")[:200]
            if first_user and first_assistant:
                break

        if not first_user:
            return None

        from langchain_core.messages import HumanMessage

        from llm.model_client import ModelClient

        llm = ModelClient(role="title", temperature=0.3)
        prompt = (
            "根据以下对话内容，生成一个不超过10个字的中文标题，只输出标题文本，不要加引号或标点。\n\n"
            f"用户: {first_user}\n"
            f"助手: {first_assistant}"
        )

        result = await llm.ainvoke([HumanMessage(content=prompt)])
        title = str(result.content).strip().strip('"\'""''')[:20]
        if not title:
            return None
        session_manager.update_title(session_id, title)
        return title
    except Exception:
        traceback.print_exc()
        return None


class DeepAgentsAgentManager:
    """Build and run DeepAgents agents for project-scoped Agent sessions."""

    def __init__(self) -> None:
        self._base_dir: Path | None = None

    def initialize(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def _resolve_workspace(
        self,
        *,
        session_id: str,
        project_id: str | None,
    ) -> tuple[Path, dict[str, Any]]:
        if project_id:
            project_path = project_registry.resolve(project_id)
            return project_path, {
                "runtime_mode": "agent",
                "project_id": project_id,
                "project_path": str(project_path),
                "workspace_type": "project",
                "workspace_path": str(project_path),
            }

        workspace_path = project_registry.ensure_unscoped_workspace(session_id)
        return workspace_path, {
            "runtime_mode": "agent",
            "project_id": None,
            "project_path": None,
            "workspace_type": "unscoped_agent",
            "workspace_path": str(workspace_path),
        }

    def _memory_dir_for(self, project_id: str | None) -> Path:
        """Return the on-disk directory that holds AGENTS.md for a project."""

        assert self._base_dir is not None
        memory_root = self._base_dir / "data" / "deepagents-memory"
        if project_id:
            return memory_root / "projects" / project_id
        return memory_root / "global"

    def _ensure_agents_md(self, memory_dir: Path) -> Path:
        """Create memory directory and a starter AGENTS.md if missing."""

        memory_dir.mkdir(parents=True, exist_ok=True)
        agents_md = memory_dir / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text(
                "# Project Memory\n\n"
                "<!--\n"
                "This file is injected into the Agent's system prompt via DeepAgents MemoryMiddleware.\n"
                "Put stable, long-lived project conventions here (tech stack, coding style, naming rules).\n"
                "Do NOT put session-specific or frequently changing data here — it hurts prompt caching.\n"
                "-->\n",
                encoding="utf-8",
            )
        return agents_md

    def _build_backend(self, workspace_path: Path):
        assert self._base_dir is not None
        skills_dir = self._base_dir / "skills"
        routes: dict[str, FilesystemBackend] = {
            "/workspace/": FilesystemBackend(root_dir=workspace_path, virtual_mode=True),
        }
        if skills_dir.exists():
            routes["/skills/"] = FilesystemBackend(root_dir=skills_dir, virtual_mode=True)
        return CompositeBackend(
            default=FilesystemBackend(root_dir=workspace_path, virtual_mode=True),
            routes=routes,
        )

    def _build_middlewares(self, project_id: str | None) -> list[Any]:
        """Build user-provided DeepAgents middlewares.

        create_deep_agent() automatically injects TodoListMiddleware and other
        base middleware. We only supply project-specific MemoryMiddleware here;
        passing TodoListMiddleware again would trigger the duplicate-instance
        assertion.
        """

        assert self._base_dir is not None

        # 1) Project-scoped or global AGENTS.md
        memory_dir = self._memory_dir_for(project_id)
        self._ensure_agents_md(memory_dir)

        # 2) Optional gstack skill index
        gstack_path = (self._base_dir / "gstack" / "AGENTS.md").resolve()
        gstack_sources: list[str] = []
        gstack_route: FilesystemBackend | None = None
        if gstack_path.exists():
            gstack_route = FilesystemBackend(
                root_dir=str(gstack_path.parent),
                virtual_mode=True,
            )
            gstack_sources.append("/gstack/AGENTS.md")

        # Use a single MemoryMiddleware with a composite backend to avoid
        # DeepAgents' "duplicate middleware instances" assertion.
        sources = ["/AGENTS.md", *gstack_sources]
        if gstack_route is not None:
            memory_backend: FilesystemBackend | CompositeBackend = CompositeBackend(
                default=FilesystemBackend(root_dir=memory_dir, virtual_mode=True),
                routes={"/gstack/": gstack_route},
            )
        else:
            memory_backend = FilesystemBackend(root_dir=memory_dir, virtual_mode=True)

        return [MemoryMiddleware(backend=memory_backend, sources=sources)]

    def _build_tools(self, workspace_path: Path) -> list[Any]:
        """Return PuddingClaw tools that do not overlap DeepAgents built-ins."""

        assert self._base_dir is not None
        tools = []
        for tool in get_all_tools(self._base_dir):
            if getattr(tool, "name", "") not in AGENT_MODE_PUDDINGCLAW_TOOLS:
                continue
            if getattr(tool, "name", "") == "terminal":
                # In Agent mode, terminal should follow the same workspace
                # boundary as the DeepAgents filesystem backend. DeepAgents
                # skills are exposed through the virtual `/skills/` backend
                # route, so terminal maps that same path to the real skills
                # directory when a skill asks to run its bundled script.
                terminal_updates = {
                    "root_dir": str(workspace_path),
                    "path_aliases": {"/skills": str(self._base_dir / "skills")},
                }
                try:
                    tool = tool.model_copy(update=terminal_updates)
                except Exception:
                    for key, value in terminal_updates.items():
                        setattr(tool, key, value)
            tools.append(tool)
        return tools

    @staticmethod
    def _build_messages(history: list[dict[str, Any]], message: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant", "system"} or not isinstance(content, str):
                continue
            entry: dict[str, Any] = {"role": role, "content": content}
            tool_calls = item.get("tool_calls")
            if role == "assistant" and tool_calls:
                # 重建 tool_calls 供模型继续上下文；思考模式下需同时回传 reasoning_content
                openai_tool_calls = []
                for tc in tool_calls:
                    tc_id = tc.get("id") or ""
                    tool_name = tc.get("tool") or tc.get("name") or "unknown_tool"
                    tool_input = tc.get("input") or tc.get("args") or {}
                    if isinstance(tool_input, dict):
                        import json
                        arguments = json.dumps(tool_input, ensure_ascii=False)
                    else:
                        arguments = str(tool_input)
                    openai_tool_calls.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": arguments},
                    })
                entry["tool_calls"] = openai_tool_calls
                if item.get("reasoning_content"):
                    entry["reasoning_content"] = item["reasoning_content"]
            messages.append(entry)
        messages.append({"role": "user", "content": message})
        return messages

    @staticmethod
    def _extract_content_text(payload: Any) -> str:
        """Extract final-answer content from common LangGraph/DeepAgents payload shapes."""
        candidate = payload
        if isinstance(payload, tuple) and payload:
            candidate = payload[0]

        content = getattr(candidate, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            text = "".join(parts)
            if text:
                return text

        return ""

    @staticmethod
    def _extract_reasoning_text(payload: Any) -> str:
        """Extract reasoning deltas without mixing them into final answer text.

        Handles multiple provider conventions:
        - DeepSeek: ``additional_kwargs["reasoning_content"]``
        - OpenAI reasoning models: ``content`` blocks of type ``thinking``
        - Responses API: ``additional_kwargs["reasoning"]`` object/summary
        """
        candidate = payload
        if isinstance(payload, tuple) and payload:
            candidate = payload[0]

        # Direct attribute (some wrappers expose reasoning_content directly)
        reasoning = getattr(candidate, "reasoning_content", None)
        if isinstance(reasoning, str):
            return reasoning

        additional = getattr(candidate, "additional_kwargs", None) or {}

        # DeepSeek-style reasoning_content
        reasoning = additional.get("reasoning_content")
        if isinstance(reasoning, str):
            return reasoning
        if isinstance(reasoning, list):
            parts = []
            for item in reasoning:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)

        # Responses API / OpenAI-style reasoning object
        reasoning = additional.get("reasoning")
        if isinstance(reasoning, str):
            return reasoning
        if isinstance(reasoning, dict):
            summary = reasoning.get("summary")
            if isinstance(summary, str):
                return summary
            if reasoning:
                return json.dumps(reasoning, ensure_ascii=False)
        if isinstance(reasoning, list):
            parts = []
            for item in reasoning:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)

        # Content blocks: OpenAI reasoning models emit thinking blocks in content
        content = getattr(candidate, "content", None)
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                    parts.append(block["thinking"])
                elif block.get("type") == "reasoning_content":
                    if isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif isinstance(block.get("reasoning"), str):
                        parts.append(block["reasoning"])
            return "".join(parts)

        return ""

    @staticmethod
    def _detect_reasoning_source(payload: Any) -> str:
        """Report which field/provider convention produced the reasoning delta."""
        candidate = payload
        if isinstance(payload, tuple) and payload:
            candidate = payload[0]

        reasoning = getattr(candidate, "reasoning_content", None)
        if isinstance(reasoning, str):
            return "attribute.reasoning_content"

        additional = getattr(candidate, "additional_kwargs", None) or {}

        reasoning = additional.get("reasoning_content")
        if isinstance(reasoning, str):
            return "additional_kwargs.reasoning_content"
        if isinstance(reasoning, list):
            return "additional_kwargs.reasoning_content[]"

        reasoning = additional.get("reasoning")
        if reasoning is not None:
            return "additional_kwargs.reasoning"

        content = getattr(candidate, "content", None)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "thinking":
                        return "content.thinking"
                    if block.get("type") == "reasoning_content":
                        return "content.reasoning_content"

        return "unknown"

    @staticmethod
    def _sse(event: str, payload: dict[str, Any]) -> dict[str, str]:
        return {
            "event": event,
            "data": json.dumps(payload, ensure_ascii=False),
        }

    @staticmethod
    def _tool_call_id(tool_call: Any) -> str:
        if isinstance(tool_call, dict):
            return str(tool_call.get("id") or "")
        return str(getattr(tool_call, "id", "") or "")

    @staticmethod
    def _tool_call_name(tool_call: Any) -> str:
        if isinstance(tool_call, dict):
            return str(tool_call.get("name") or tool_call.get("tool") or "unknown_tool")
        return str(getattr(tool_call, "name", None) or getattr(tool_call, "tool", None) or "unknown_tool")

    @staticmethod
    def _tool_call_args(tool_call: Any) -> Any:
        if isinstance(tool_call, dict):
            return tool_call.get("args", tool_call.get("input", {}))
        return getattr(tool_call, "args", getattr(tool_call, "input", {}))

    @staticmethod
    def _format_tool_input(value: Any, *, limit: int = 2000) -> str:
        try:
            if isinstance(value, str):
                text = value
            else:
                text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        return text[:limit]

    @staticmethod
    def _tool_message_name(tool_msg: Any, pending: dict[str, dict[str, str]]) -> str:
        name = getattr(tool_msg, "name", None)
        if name:
            return str(name)
        tc_id = str(getattr(tool_msg, "tool_call_id", "") or "")
        return pending.get(tc_id, {}).get("tool", "unknown_tool")

    @staticmethod
    def _tool_message_output(tool_msg: Any) -> str:
        content = getattr(tool_msg, "content", "")
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            return str(content)

    @staticmethod
    def _is_tool_error(tool_msg: Any, output: str) -> bool:
        status = getattr(tool_msg, "status", None)
        if status == "error":
            return True
        return output.lstrip().lower().startswith(("error:", "exception:", "traceback"))

    @staticmethod
    def _metadata_node(metadata: Any) -> str:
        if isinstance(metadata, dict):
            return str(metadata.get("langgraph_node") or "")
        return ""

    @staticmethod
    def _segment_has_payload(segment: dict[str, Any]) -> bool:
        return bool(segment.get("content") or segment.get("tool_calls") or segment.get("sources"))

    @staticmethod
    def _append_reasoning_to_timeline(segment: dict[str, Any], text: str) -> None:
        """Append reasoning text to the current reasoning item, or create one."""
        if not text:
            return
        timeline = segment.setdefault("timeline", [])
        current = segment.get("_current_reasoning")
        if current is None:
            current = {
                "type": "reasoning",
                "content": "",
                "id": f"reasoning-{len(timeline)}",
            }
            timeline.append(current)
            segment["_current_reasoning"] = current
        current["content"] += text

    @staticmethod
    def _finalize_reasoning_timeline(segment: dict[str, Any]) -> None:
        """Close the current reasoning chunk so the next reasoning starts a new item."""
        segment["_current_reasoning"] = None

    @staticmethod
    def _add_tool_start_to_timeline(
        segment: dict[str, Any],
        tool_call_id: str,
        tool_name: str,
        tool_input: str,
    ) -> None:
        """Add a tool_start item to the timeline."""
        timeline = segment.setdefault("timeline", [])
        timeline.append(
            {
                "type": "tool",
                "tool_call": {
                    "id": tool_call_id,
                    "tool": tool_name,
                    "input": tool_input,
                    "status": "running",
                },
                "id": tool_call_id or f"tool-{len(timeline)}",
            }
        )

    @staticmethod
    def _update_tool_end_in_timeline(
        segment: dict[str, Any],
        tool_call_id: str,
        output: str,
        is_error: bool,
    ) -> None:
        """Update the matching tool item in the timeline with its result."""
        timeline = segment.get("timeline", [])
        for item in reversed(timeline):
            if item.get("type") == "tool":
                tc = item.get("tool_call", {})
                if tc.get("id") == tool_call_id:
                    tc["output"] = output
                    tc["is_error"] = is_error
                    tc["status"] = "error" if is_error else "completed"
                    break

    @staticmethod
    def _last_ai_content(state: dict[str, Any] | None) -> str:
        if not state:
            return ""
        messages = state.get("messages") or []
        for msg in reversed(messages):
            msg_type = getattr(msg, "type", None)
            if msg_type not in {None, "ai"}:
                continue
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content:
                return content
        return ""

    async def astream(
        self,
        *,
        message: str,
        session_id: str,
        project_id: str | None = None,
        user_id: str = "default_user",
    ) -> AsyncGenerator[dict[str, str], None]:
        """Stream Agent-mode SSE events compatible with the existing frontend."""

        try:
            thinking_enabled = bool(config.load_config().get("thinking_mode", False))
            logger.info("Agent stream thinking_mode=%s for session=%s", thinking_enabled, session_id)

            workspace_path, metadata = self._resolve_workspace(
                session_id=session_id,
                project_id=project_id,
            )
            session_manager.update_metadata(session_id, metadata)

            history = session_manager.load_session_for_agent(session_id)
            is_first_message = not any(item.get("role") == "user" for item in history)
            messages = self._build_messages(history, message)
            historical_tool_call_ids = {
                tc.get("id")
                for msg in messages
                for tc in msg.get("tool_calls") or []
                if tc.get("id")
            }

            model = ModelClientChatModel(role="agent", streaming=True)
            logger.info("Building DeepAgents agent for session=%s project=%s", session_id, project_id)
            agent = create_deep_agent(
                model=model,
                tools=self._build_tools(workspace_path),
                skills=["/skills/"],
                middleware=self._build_middlewares(project_id),
                backend=self._build_backend(workspace_path),
                system_prompt=(
                    "You are PuddingClaw Agent mode. The filesystem tools are scoped to the current workspace. "
                    "Project-level memory and the gstack skill index have been injected via MemoryMiddleware. "
                    "Do not claim access to files outside this workspace unless an external-file permission flow "
                    "grants it.\n\n"
                    "When the user asks you to break a task into steps or track progress, call the `write_todos` "
                    "tool to create a structured todo list.\n\n"
                    "### 来源引用规则\n"
                    "- 检索类工具返回的结果中可能包含稳定的 `source_id`。\n"
                    "- 当回答中的具体论述使用了某个来源的信息时，必须在该论述后紧跟标记 `[^source_id]`。\n"
                    "- 只能引用工具实际提供的 `source_id`，禁止编造来源、文件名、URL 或页码。\n"
                    "- 如果某来源未被用于支撑最终回答，不要为它添加引用标记。\n"
                    "- 禁止只写『来源』等裸词而不带 `[^source_id]` 标记。"
                ),
            )
            logger.info("DeepAgents agent built successfully for session=%s", session_id)

            emitted_text = ""
            final_state: dict[str, Any] | None = None
            tools_just_finished = False
            emitted_tool_starts: set[str] = set()
            pending_tool_starts: dict[str, dict[str, str]] = {}
            turn_sources: list[dict[str, Any]] = []

            def new_segment() -> dict[str, Any]:
                return {
                    "content": "",
                    "tool_calls": [],
                    "timeline": [],
                    "reasoning_content": "",
                    "_current_reasoning": None,
                }

            segments: list[dict[str, Any]] = [new_segment()]
            active_segment = segments[0]
            chunk_count = 0
            emitted_reasoning = False
            accumulated_reasoning = ""
            reasoning_log_chars = 0
            REASONING_LOG_INTERVAL = 500
            async for item in agent.astream(
                {"messages": messages},
                stream_mode=["messages", "updates", "custom", "values"],
                config={"configurable": {"thread_id": session_id, "user_id": user_id}},
            ):
                chunk_count += 1
                if chunk_count <= 5 or chunk_count % 20 == 0:
                    logger.info(
                        "Received stream chunk #%d for session=%s: %s",
                        chunk_count,
                        session_id,
                        type(item).__name__,
                    )
                mode: str | None = None
                payload: Any = item
                if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                    mode, payload = item

                if mode == "messages" or mode is None:
                    message_payload = payload[0] if isinstance(payload, tuple) and payload else payload
                    metadata = payload[1] if isinstance(payload, tuple) and len(payload) > 1 else {}
                    text = self._extract_content_text(payload)
                    reasoning_text = self._extract_reasoning_text(payload)
                    if reasoning_text and thinking_enabled:
                        node = self._metadata_node(metadata)
                        if not node or node == "model":
                            emitted_reasoning = True
                            accumulated_reasoning += reasoning_text
                            active_segment["reasoning_content"] += reasoning_text
                            self._append_reasoning_to_timeline(active_segment, reasoning_text)
                            source = self._detect_reasoning_source(payload)
                            prev_logged_chars = reasoning_log_chars
                            reasoning_log_chars = len(accumulated_reasoning)
                            if reasoning_log_chars // REASONING_LOG_INTERVAL != prev_logged_chars // REASONING_LOG_INTERVAL:
                                logger.info(
                                    "Emitting reasoning delta for session=%s (node=%s, source=%s, accumulated=%d): %s...",
                                    session_id,
                                    node,
                                    source,
                                    reasoning_log_chars,
                                    accumulated_reasoning[-120:].replace("\n", " "),
                                )
                            yield self._sse(
                                "reasoning",
                                {
                                    "status": "delta",
                                    "content": reasoning_text,
                                    "chars": len(reasoning_text),
                                },
                            )
                    if text:
                        if (
                            isinstance(message_payload, AIMessageChunk)
                            and getattr(message_payload, "tool_calls", None)
                        ):
                            # Tool-call chunks are rendered as tool cards via
                            # the following `updates` stream, not assistant text.
                            continue
                        node = self._metadata_node(metadata)
                        if node and node != "model":
                            continue
                        self._finalize_reasoning_timeline(active_segment)
                        if tools_just_finished:
                            tools_just_finished = False
                            # The model has been re-invoked after tool calls.
                            # Start a new segment so the frontend can render each
                            # model invocation + its tools as a separate block.
                            active_segment = new_segment()
                            segments.append(active_segment)
                            yield self._sse("segment_break", {})
                        active_segment["content"] += text
                        emitted_text += text
                        yield self._sse("token", {"content": text})
                elif mode == "updates" and isinstance(payload, dict):
                    for node_name, node_data in payload.items():
                        node_messages = node_data.get("messages") if isinstance(node_data, dict) else None
                        if not node_messages:
                            continue

                        if node_name == "tools":
                            for tool_msg in node_messages:
                                tc_id = str(getattr(tool_msg, "tool_call_id", "") or "")
                                tool_name = self._tool_message_name(tool_msg, pending_tool_starts)
                                original_output = self._tool_message_output(tool_msg)
                                pending_tool = pending_tool_starts.get(tc_id, {})
                                adapted = tool_result_adapter.adapt(
                                    original_output,
                                    tool_name=tool_name,
                                    tool_input=pending_tool.get("input", ""),
                                    tool_call_id=tc_id,
                                )
                                raw_output = adapted.answer_context
                                sources = adapted.sources
                                logger.info(
                                    "Tool %s adapted sources: %d (output preview: %s)",
                                    tool_name,
                                    len(sources),
                                    raw_output[:100].replace("\n", " "),
                                )
                                if sources:
                                    try:
                                        tool_msg.content = format_sources_for_model(raw_output, sources)
                                    except Exception:
                                        pass
                                    turn_sources = dedupe_sources(turn_sources + sources)
                                    for source in sources:
                                        logger.info(
                                            "Emitting source_found event: source_id=%s",
                                            source.get("source_id"),
                                        )
                                        yield self._sse(
                                            "source_found",
                                            {
                                                "tool_call_id": tc_id,
                                                "source": source,
                                            },
                                        )
                                is_error = self._is_tool_error(tool_msg, raw_output)
                                self._update_tool_end_in_timeline(active_segment, tc_id or "", raw_output, is_error)
                                pending_tool_starts.pop(tc_id, None)

                                matched = False
                                if tc_id:
                                    for tc in active_segment["tool_calls"]:
                                        if tc.get("id") == tc_id and "output" not in tc:
                                            tc["output"] = raw_output
                                            tc["raw_output"] = original_output
                                            tc["is_error"] = is_error
                                            if sources:
                                                tc["sources"] = sources
                                            matched = True
                                            break
                                if not matched:
                                    active_segment["tool_calls"].append(
                                        {
                                            "tool": tool_name,
                                            "input": "",
                                            "id": tc_id,
                                            "output": raw_output,
                                            "raw_output": original_output,
                                            "is_error": is_error,
                                            **({"sources": sources} if sources else {}),
                                        }
                                    )
                                yield self._sse(
                                    "tool_end",
                                    {
                                        "tool": tool_name,
                                        "id": tc_id,
                                        "output": raw_output[:4000],
                                        "output_full_length": len(raw_output),
                                        "summary_source": None,
                                        "is_error": is_error,
                                        "sources": sources,
                                    },
                                )
                                tools_just_finished = True
                        else:
                            for agent_msg in node_messages:
                                tool_calls = getattr(agent_msg, "tool_calls", None) or []
                                for tool_call in tool_calls:
                                    tc_id = self._tool_call_id(tool_call)
                                    if tc_id and tc_id in historical_tool_call_ids:
                                        # Skip tool calls that originate from input history;
                                        # they should not appear in the current turn timeline.
                                        continue
                                    if tc_id and tc_id in emitted_tool_starts:
                                        continue
                                    tool_name = self._tool_call_name(tool_call)
                                    tool_input = self._format_tool_input(self._tool_call_args(tool_call))
                                    if tc_id:
                                        emitted_tool_starts.add(tc_id)
                                        pending_tool_starts[tc_id] = {
                                            "tool": tool_name,
                                            "input": tool_input,
                                        }
                                    self._finalize_reasoning_timeline(active_segment)
                                    active_segment["tool_calls"].append(
                                        {
                                            "tool": tool_name,
                                            "input": tool_input,
                                            "id": tc_id,
                                        }
                                    )
                                    self._add_tool_start_to_timeline(
                                        active_segment, tc_id or "", tool_name, tool_input
                                    )
                                    yield self._sse(
                                        "tool_start",
                                        {
                                            "tool": tool_name,
                                            "input": tool_input,
                                            "id": tc_id,
                                        },
                                    )
                elif mode == "custom" and isinstance(payload, dict):
                    event_type = str(payload.get("type") or "")
                    if event_type:
                        yield self._sse(event_type, payload)
                elif mode == "values" and isinstance(payload, dict):
                    final_state = payload

            final_content = self._last_ai_content(final_state) or emitted_text
            if final_content:
                current_text = active_segment.get("content", "")
                if not current_text.strip():
                    active_segment["content"] = final_content
                    emitted_text = final_content
                    yield self._sse("token", {"content": final_content})
                elif final_content.strip() not in current_text:
                    # The authoritative final answer differs from the streamed
                    # text (e.g. only intermediate planning was streamed before
                    # tools). Replace with the final answer.
                    active_segment["content"] = final_content
                    emitted_text = final_content
                    yield self._sse("token", {"content": final_content})
            elif emitted_reasoning and not final_content:
                diagnostic = (
                    "模型本轮只返回了 reasoning_content，没有返回正式回答 content。"
                    "请检查 Higress 路由模型是否应切换为非推理模型，或确认 provider 是否会在流结束前输出 content。"
                )
                active_segment["content"] += diagnostic
                final_content = diagnostic
                yield self._sse("token", {"content": diagnostic})

            for tc_id, pending in list(pending_tool_starts.items()):
                failed_output = "Tool execution did not return a result before the agent finished."
                active_segment["tool_calls"].append(
                    {
                        "tool": pending.get("tool", "unknown_tool"),
                        "input": pending.get("input", ""),
                        "id": tc_id,
                        "output": failed_output,
                        "raw_output": failed_output,
                        "summary_source": "missing_tool_output",
                        "is_error": True,
                    }
                )
                yield self._sse(
                    "tool_end",
                    {
                        "tool": pending.get("tool", "unknown_tool"),
                        "id": tc_id,
                        "output": failed_output,
                        "output_full_length": len(failed_output),
                        "summary_source": "missing_tool_output",
                        "is_error": True,
                        "sources": [],
                    },
                )

            session_manager.save_message(session_id, "user", message)
            # Build the single assistant message content by concatenating segment
            # text, and persist the segments array for the UI.
            full_content = "\n\n".join(
                seg["content"] for seg in segments if seg.get("content")
            )
            all_tool_calls = [tc for seg in segments for tc in seg.get("tool_calls", [])]
            all_timeline = [item for seg in segments for item in seg.get("timeline", [])]
            final_citations = finalize_citations(full_content, turn_sources)
            for seg in segments:
                seg.pop("_current_reasoning", None)
            logger.info(
                "Stream summary for session=%s: chunks=%d, reasoning_emitted=%s, reasoning_len=%d, text_len=%d, segments=%d",
                session_id,
                chunk_count,
                emitted_reasoning,
                len(accumulated_reasoning),
                len(emitted_text),
                len(segments),
            )
            if self._segment_has_payload({"content": full_content, "tool_calls": all_tool_calls}):
                session_manager.save_message(
                    session_id,
                    "assistant",
                    full_content,
                    tool_calls=all_tool_calls or None,
                    sources=dedupe_sources(turn_sources) or None,
                    citations=final_citations or None,
                    reasoning_content=accumulated_reasoning or None,
                    timeline=all_timeline or None,
                    segments=segments or None,
                )
            yield self._sse(
                "citations_finalized",
                {
                    "citations": final_citations,
                    "cited_source_ids": list(dict.fromkeys(
                        citation["source_id"] for citation in final_citations
                    )),
                },
            )
            yield self._sse(
                "done",
                {
                    "content": final_content,
                    "session_id": session_id,
                    "project_id": project_id,
                    "workspace_path": str(workspace_path),
                },
            )
            logger.info("Stream finished for session=%s with %d chunks", session_id, chunk_count)
            if is_first_message:
                title = await _generate_title(session_id)
                if title:
                    yield self._sse("title", {"session_id": session_id, "title": title})
        except Exception as exc:
            logger.exception("Agent stream failed for session=%s: %s", session_id, exc)
            traceback.print_exc()
            error_msg = str(exc) or exc.__class__.__name__
            yield self._sse("error", {"error": error_msg, "message": error_msg})


deepagents_agent_manager = DeepAgentsAgentManager()
