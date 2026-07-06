"""DeepAgents runtime manager for Agent mode."""

from __future__ import annotations

import json
import logging
import os
import re
import base64
import time
import traceback
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import quote

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.subagents import SubAgent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from graph.citations import dedupe_sources, finalize_citations, format_sources_for_model
from graph.deepagents_prompt_builder import build_deepagents_system_prompt
from graph.attachment_store import attachment_store
from graph.managed_paths import is_managed_resource_path
from graph.session_manager import session_manager
from knowledge.paths import get_knowledge_root
from graph.permission_middleware import ExternalFilePermissionMiddleware
from graph.permission_resume import permission_resume_registry
from graph.tool_result_adapter import tool_result_adapter
from graph.trace_collector import TraceCollector, TraceSpan
from graph.middleware_trace_proxy import wrap_middlewares_for_trace
from knowledge.paths import get_knowledge_root
from llm.model_client import ModelClientChatModel
from projects.registry import project_registry
from tools import get_all_tools
import config

logger = logging.getLogger(__name__)

AGENT_MODE_PUDDINGCLAW_TOOLS = {
    "terminal",
    "read_resource",
    "fetch_url",
    "tavily_search",
    "llamaindex_knowledge_query",
    "pandas_knowledge_query",
}

DEFAULT_IMAGE_ANALYZER_PROMPT = (
    "You are an image analysis specialist. When given an image, describe its contents in detail "
    "and answer any questions about it. Return your findings as concise, structured text."
)

IMAGE_PATH_RE = re.compile(
    r"(?P<path>(?:~|/|[A-Za-z]:[\\/])(?:[^\s'\"<>]|\\ )+\.(?:png|jpe?g|webp|gif|bmp|tiff?))",
    re.IGNORECASE,
)
LOCAL_RESOURCE_PATH_RE = re.compile(
    r"(?P<path>(?:~|/|[A-Za-z]:[\\/])(?:[^\s'\"<>]|\\ )+\."
    r"(?:md|markdown|txt|json|ya?ml|csv|tsv|xlsx?|pdf|docx?|pptx?|png|jpe?g|webp|gif|bmp|tiff?))",
    re.IGNORECASE,
)
IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _resolve_subagent_model(model_name: str) -> str | BaseChatModel:
    """Resolve a subagent model spec.

    - Strings containing a colon (e.g. "qwen:qwen3.7") are passed through for
      LangChain init_chat_model resolution (direct provider).
    - Bare model names that match a Higress AI route (e.g. "qwen3.7-plus") are
      wired to the gateway via ChatOpenAI.
    - Anything else is returned as-is.
    """
    if ":" in model_name:
        return model_name

    try:
        from higress_config_reader import get_higress_routed_models
        from capabilities import get_effective_gateway_url
        from langchain_openai import ChatOpenAI

        routed = set(get_higress_routed_models())
        if model_name in routed:
            gateway_url = get_effective_gateway_url()
            if gateway_url:
                return ChatOpenAI(
                    model=model_name,
                    api_key="puddingclaw-gateway",
                    base_url=gateway_url,
                    streaming=True,
                )
    except Exception as exc:
        logger.debug("[subagent] failed to resolve gateway model %r: %s", model_name, exc)
    return model_name


def _is_image_analyzer_item(item: dict[str, Any]) -> bool:
    return str(item.get("name") or "") == "image_analyzer" or str(item.get("route_trigger") or "") == "image_input"


class AttachmentImageContentMiddleware(AgentMiddleware[Any, Any, Any]):
    """Materialize image resources only after the subagent reads them with a tool."""

    IMAGE_ATTACHMENT_RE = re.compile(r"^PuddingClaw-Resource-Image:\s*(att_[A-Za-z0-9_-]+)\s*$", re.MULTILINE)
    IMAGE_PATH_RE = re.compile(r"^PuddingClaw-Resource-Image-Path:\s*(.+?)\s*$", re.MULTILINE)

    @staticmethod
    def _session_id_from_text(text: str) -> str:
        match = re.search(r"harness_attachment_session_id:\s*([A-Za-z0-9_.:-]+)", text)
        if not match:
            match = re.search(r"harness_image_session_id:\s*([A-Za-z0-9_.:-]+)", text)
        return match.group(1) if match else ""

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return str(content or "")

    def _image_inputs(self, request: ModelRequest[Any]) -> list[dict[str, Any]]:
        all_text = "\n".join(
            self._content_text(getattr(message, "content", ""))
            for message in getattr(request, "messages", [])
        )
        state = getattr(request, "state", {}) or {}
        session_id = (
            str(state.get("harness_attachment_session_id") or "")
            or str(state.get("harness_image_session_id") or "")
            or self._session_id_from_text(all_text)
        )
        tool_text = "\n".join(
            self._content_text(getattr(message, "content", ""))
            for message in getattr(request, "messages", [])
            if isinstance(message, ToolMessage)
        )

        image_inputs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for attachment_id in self.IMAGE_ATTACHMENT_RE.findall(tool_text):
            if not session_id or attachment_id in seen:
                continue
            seen.add(attachment_id)
            item = attachment_store.get(session_id, attachment_id)
            if not item or item.get("type") != "image":
                continue
            block = DeepAgentsAgentManager._image_content_from_attachment(
                {"id": attachment_id, "type": "image"},
                session_id=session_id,
            )
            url = str((block or {}).get("image_url", {}).get("url") or "")
            if not url:
                continue
            image_inputs.append(
                {
                    "ref": f"image_{len(image_inputs) + 1}",
                    "id": attachment_id,
                    "name": str(item.get("name") or attachment_id),
                    "url": url,
                    "source": "attachment",
                }
            )
        for raw_path in self.IMAGE_PATH_RE.findall(tool_text):
            path_key = raw_path.strip()
            if not path_key or path_key in seen:
                continue
            seen.add(path_key)
            block = DeepAgentsAgentManager._image_content_from_path(path_key, session_id=session_id)
            url = str((block or {}).get("image_url", {}).get("url") or "")
            if not url:
                continue
            image_inputs.append(
                {
                    "ref": f"image_{len(image_inputs) + 1}",
                    "name": Path(path_key).name,
                    "path": path_key,
                    "url": url,
                    "source": "local_path",
                }
            )
        return image_inputs

    def _request_with_images(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        image_inputs = self._image_inputs(request)
        if not image_inputs:
            return request
        messages = list(request.messages)
        if not messages:
            return request

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "你刚刚通过 read_resource 打开了图片资源。请分析随附图片，并返回给主 Agent "
                    "可直接使用的中文结构化结果。"
                ),
            }
        ]
        for image in image_inputs:
            url = str(image.get("url") or "")
            if url.startswith("data:image/"):
                content.append({"type": "image_url", "image_url": {"url": url}})

        messages.append(HumanMessage(content=content))
        return request.override(messages=messages)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._request_with_images(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._request_with_images(request))


def _build_subagent_item(item: dict[str, Any], default_tools: list[Any], default_skills: list[str]) -> SubAgent:
    """Build a single SubAgent spec from a settings item."""
    name = item.get("name", "subagent") or "subagent"
    model_name = item.get("model", "") or ""
    model = _resolve_subagent_model(model_name) if model_name else None
    description = item.get("description") or f"Subagent `{name}`."
    route_trigger = str(item.get("route_trigger") or "").strip()
    if route_trigger and route_trigger not in description:
        description = f"{description} Use this subagent when the main request matches this routing hint: `{route_trigger}`."
    is_image_analyzer = _is_image_analyzer_item(item)
    native_hint = (
        "Use this subagent via the native task tool whenever the user provides image attachments "
        "or local image paths. Preserve the harness_attachment_session_id and image refs from the user "
        "message in the task description."
    )
    if is_image_analyzer and native_hint not in description:
        description = f"{description} {native_hint}"
    system_prompt = item.get("system_prompt") or DEFAULT_IMAGE_ANALYZER_PROMPT
    if is_image_analyzer:
        system_prompt = (
            f"{system_prompt}\n\n"
            "When the task description includes attachment refs like `att_xxx` or local image paths, "
            "first call `read_resource` for each image resource. Do not answer image-content questions "
            "until the resource has been read. After `read_resource` returns an image resource marker, "
            "continue with visual analysis and return concise structured findings to the main Agent."
        )

    tools_cfg = item.get("tools", {})
    tools = default_tools if tools_cfg.get("mode", "inherit") == "inherit" else []

    skills_cfg = item.get("skills", {})
    if skills_cfg.get("mode", "inherit") == "inherit":
        skills = list(default_skills)
    else:
        configured_paths = skills_cfg.get("paths", [])
        skills = [str(path) for path in configured_paths if str(path).strip()]

    spec: SubAgent = {
        "name": name,
        "description": description,
        "system_prompt": system_prompt,
        "tools": tools,
    }
    if model:
        spec["model"] = model
    if skills:
        spec["skills"] = skills
    if is_image_analyzer:
        spec["middleware"] = [AttachmentImageContentMiddleware()]
    return spec


def _build_subagents(default_tools: list[Any], default_skills: list[str]) -> list[SubAgent]:
    """Build declarative subagents from normalized settings config."""
    items = config.get_settings_for_display().get("subagents", {}).get("items", [])
    if not items:
        return []

    subagents: list[SubAgent] = []
    for item in items:
        if not item.get("enabled", False):
            continue
        subagents.append(_build_subagent_item(item, default_tools, default_skills))
    return subagents


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
        self._checkpointer: Any | None = None
        self._checkpointer_cm: Any | None = None
        self._checkpointer_info: dict[str, Any] | None = None

    def initialize(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        # LangGraph checkpoint SQLite recommends strict msgpack encoding.
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

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
        knowledge_dir = get_knowledge_root(self._base_dir)
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        routes: dict[str, FilesystemBackend] = {
            "/workspace/": FilesystemBackend(root_dir=workspace_path, virtual_mode=True),
            "/knowledge/": FilesystemBackend(root_dir=knowledge_dir, virtual_mode=True),
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

        middlewares: list[Any] = [
            MemoryMiddleware(backend=memory_backend, sources=sources),
            ExternalFilePermissionMiddleware(),
        ]
        model_call_limit_cfg = config.load_config().get("harness", {}).get("model_call_limit", {})
        if model_call_limit_cfg.get("enabled", True):
            run_limit = model_call_limit_cfg.get("run_limit")
            thread_limit = model_call_limit_cfg.get("thread_limit")
            exit_behavior = str(model_call_limit_cfg.get("exit_behavior") or "end")
            if exit_behavior not in {"end", "error"}:
                exit_behavior = "end"
            if isinstance(run_limit, int) and run_limit <= 0:
                run_limit = None
            if isinstance(thread_limit, int) and thread_limit <= 0:
                thread_limit = None
            if run_limit is not None or thread_limit is not None:
                middlewares.append(
                    ModelCallLimitMiddleware(
                        run_limit=run_limit if isinstance(run_limit, int) else None,
                        thread_limit=thread_limit if isinstance(thread_limit, int) else None,
                        exit_behavior=exit_behavior,  # type: ignore[arg-type]
                    )
                )
        return middlewares

    async def _build_checkpointer(self) -> Any:
        """Return a process-wide checkpointer for HITL interrupt/resume.

        Production/runtime uses SQLite so pending interrupts survive normal
        request boundaries and backend restarts. Local test environments may not
        have the optional plugin installed yet, so fall back to MemorySaver while
        recording that fact in runtime inventory.
        """

        if self._checkpointer is not None:
            return self._checkpointer

        assert self._base_dir is not None
        checkpoint_dir = self._base_dir / "data" / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        db_path = checkpoint_dir / "deepagents.sqlite"

        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            cm = AsyncSqliteSaver.from_conn_string(str(db_path))
            checkpointer = await cm.__aenter__()
            setup = getattr(checkpointer, "setup", None)
            if callable(setup):
                await setup()
            self._checkpointer_cm = cm
            self._checkpointer = checkpointer
            self._checkpointer_info = {
                "type": "async_sqlite",
                "path": str(db_path),
                "strict_msgpack": os.environ.get("LANGGRAPH_STRICT_MSGPACK") == "true",
            }
            return checkpointer
        except Exception as exc:
            logger.warning("SQLite LangGraph checkpointer unavailable, falling back to memory: %s", exc)
            from langgraph.checkpoint.memory import InMemorySaver

            self._checkpointer = InMemorySaver()
            self._checkpointer_info = {
                "type": "memory",
                "fallback_reason": str(exc),
                "strict_msgpack": os.environ.get("LANGGRAPH_STRICT_MSGPACK") == "true",
            }
            return self._checkpointer

    def _build_tools(self, workspace_path: Path, session_id: str = "") -> list[Any]:
        """Return PuddingClaw tools that do not overlap DeepAgents built-ins."""

        assert self._base_dir is not None
        tools = []
        for tool in get_all_tools(self._base_dir):
            if getattr(tool, "name", "") not in AGENT_MODE_PUDDINGCLAW_TOOLS:
                continue
            if getattr(tool, "name", "") == "terminal":
                # In Agent mode, terminal should follow the same workspace
                # boundary as the DeepAgents filesystem backend. Map the
                # virtual `/workspace/` and `/skills/` prefixes to real host
                # directories so shell commands use the same paths as
                # read_file/write_file.
                terminal_updates = {
                    "root_dir": str(workspace_path),
                    "path_aliases": {
                        "/workspace": str(workspace_path),
                        "/knowledge": str(get_knowledge_root(self._base_dir)),
                        "/skills": str(self._base_dir / "skills"),
                    },
                }
                try:
                    tool = tool.model_copy(update=terminal_updates)
                except Exception:
                    for key, value in terminal_updates.items():
                        setattr(tool, key, value)
            elif getattr(tool, "name", "") == "read_resource":
                resource_updates = {
                    "session_id": session_id,
                    "workspace_path": str(workspace_path),
                }
                try:
                    tool = tool.model_copy(update=resource_updates)
                except Exception:
                    for key, value in resource_updates.items():
                        setattr(tool, key, value)

            tools.append(tool)
        return tools

    @staticmethod
    def _middleware_hooks(middleware: Any) -> list[str]:
        try:
            from langchain.agents.middleware import AgentMiddleware
        except Exception:
            AgentMiddleware = object  # type: ignore[assignment]

        hooks: list[str] = []
        for hook in (
            "before_agent",
            "before_model",
            "after_model",
            "after_agent",
            "wrap_model_call",
            "wrap_tool_call",
        ):
            for cls in type(middleware).mro():
                if cls is AgentMiddleware:
                    break
                if hook in cls.__dict__ or f"a{hook}" in cls.__dict__:
                    hooks.append(hook)
                    break
        return hooks

    @classmethod
    def _middleware_entry(
        cls,
        middleware: Any,
        *,
        order: int,
        source: str,
        note: str = "",
    ) -> dict[str, Any]:
        return {
            "name": middleware if isinstance(middleware, str) else middleware.__class__.__name__,
            "order": order,
            "source": source,
            "hooks": cls._middleware_hooks(middleware) if not isinstance(middleware, str) else [],
            "note": note,
        }

    @classmethod
    def _middleware_inventory(cls, user_middlewares: list[Any], skills: list[str]) -> dict[str, Any]:
        """Describe the DeepAgents/LangChain middleware stack in execution order."""

        stack: list[dict[str, Any]] = []

        def add(name: str, source: str, hooks: list[str], note: str = "") -> None:
            stack.append(
                {
                    "name": name,
                    "order": len(stack) + 1,
                    "source": source,
                    "hooks": hooks,
                    "note": note,
                }
            )

        add(
            "TodoListMiddleware",
            "deepagents.base",
            ["wrap_model_call", "after_model"],
            "注入 write_todos 提示并检查 todo tool call",
        )
        if skills:
            add("SkillsMiddleware", "deepagents.base", ["before_agent"], "将 skills snapshot 注入系统上下文")
        add("FilesystemMiddleware", "deepagents.base", ["wrap_tool_call"], "提供 /workspace 与 /skills 文件系统能力")
        add("SubAgentMiddleware", "deepagents.base", ["wrap_model_call"], "向 system message 注入 task/subagent 使用说明")
        add("SummarizationMiddleware", "deepagents.base", ["before_model"], "DeepAgents 基础上下文压缩")
        add("PatchToolCallsMiddleware", "deepagents.base", ["after_model"], "修正/补齐工具调用")

        for middleware in user_middlewares:
            entry = cls._middleware_entry(
                middleware,
                order=len(stack) + 1,
                source="puddingclaw.user",
                note="PuddingClaw 运行时显式挂载",
            )
            if not entry["hooks"] and entry["name"] == "MemoryMiddleware":
                entry["hooks"] = ["before_agent"]
            stack.append(entry)

        add("AnthropicPromptCachingMiddleware", "deepagents.tail", ["wrap_model_call"], "DeepAgents tail stack，非 Anthropic 模型下通常 no-op")

        hook_order: dict[str, list[dict[str, Any]]] = {}
        for hook in (
            "before_agent",
            "before_model",
            "after_model",
            "after_agent",
            "wrap_model_call",
            "wrap_tool_call",
        ):
            entries = [entry for entry in stack if hook in entry.get("hooks", [])]
            if hook in {"after_model", "after_agent"}:
                entries = list(reversed(entries))
            hook_order[hook] = [
                {
                    "name": entry["name"],
                    "stack_order": entry["order"],
                    "execution_order": idx + 1,
                    "source": entry["source"],
                    "note": entry.get("note", ""),
                }
                for idx, entry in enumerate(entries)
            ]

        return {
            "stack": stack,
            "hooks": hook_order,
            "order_rule": {
                "before": "before_agent / before_model 按 stack order 正序执行",
                "after": "after_model / after_agent 按 stack order 反序执行",
                "wrap": "wrap_model_call / wrap_tool_call 按 stack order 进入，外层包住内层",
            },
        }

    @staticmethod
    def _tool_inventory(tools: list[Any]) -> list[dict[str, Any]]:
        mounted = [
            {"name": "write_todos", "source": "deepagents.builtin", "description": "管理 todo list"},
            {"name": "ls", "source": "deepagents.builtin", "description": "列出文件"},
            {"name": "read_file", "source": "deepagents.builtin", "description": "读取文件"},
            {"name": "write_file", "source": "deepagents.builtin", "description": "写入文件"},
            {"name": "edit_file", "source": "deepagents.builtin", "description": "编辑文件"},
            {"name": "glob", "source": "deepagents.builtin", "description": "按 glob 查找文件"},
            {"name": "grep", "source": "deepagents.builtin", "description": "全文检索文件"},
            {"name": "execute", "source": "deepagents.builtin", "description": "执行 shell 命令"},
            {"name": "task", "source": "deepagents.builtin", "description": "调用 subagent"},
        ]
        seen = {item["name"] for item in mounted}
        for tool in tools:
            name = str(getattr(tool, "name", "") or tool.__class__.__name__)
            if not name or name in seen:
                continue
            mounted.append(
                {
                    "name": name,
                    "source": "puddingclaw.tool",
                    "description": str(getattr(tool, "description", "") or "")[:240],
                }
            )
            seen.add(name)
        return mounted

    @staticmethod
    def _subagent_inventory() -> list[dict[str, Any]]:
        try:
            items = config.get_settings_for_display().get("subagents", {}).get("items", [])
        except Exception:
            items = []
        mounted: list[dict[str, Any]] = [
            {
                "name": "general-purpose",
                "enabled": True,
                "model": "inherits main agent",
                "description": (
                    "DeepAgents default general-purpose subagent, automatically injected "
                    "through SubAgentMiddleware when not overridden by config."
                ),
                "route_trigger": "complex independent task",
                "tools_mode": "inherit",
                "skills_mode": "inherit",
                "source": "deepagents.default",
                "href": "",
            }
        ]
        for index, item in enumerate(items):
            name = str(item.get("name") or f"subagent_{index + 1}")
            if name == "general-purpose":
                mounted = [entry for entry in mounted if entry.get("source") != "deepagents.default"]
            tools_cfg = item.get("tools") or {}
            skills_cfg = item.get("skills") or {}
            mounted.append(
                {
                    "name": name,
                    "enabled": bool(item.get("enabled", False)),
                    "model": str(item.get("model") or ""),
                    "description": str(item.get("description") or "")[:240],
                    "route_trigger": str(item.get("route_trigger") or ""),
                    "tools_mode": str(tools_cfg.get("mode") or "inherit"),
                    "skills_mode": str(skills_cfg.get("mode") or "inherit"),
                    "source": "config",
                    "href": f"/settings?category=harness&tab=subagent&subagent={quote(name)}",
                }
            )
        return mounted

    @staticmethod
    def _extract_skill_frontmatter(text: str, fallback_name: str) -> dict[str, str]:
        name = fallback_name
        description = ""
        if text.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", text, flags=re.S)
            if match:
                for line in match.group(1).splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip("'\"") or name
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("'\"")
        if not description:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                    description = stripped[:240]
                    break
        return {"name": name, "description": description}

    def _skills_inventory(self) -> list[dict[str, Any]]:
        assert self._base_dir is not None
        skills_dir = self._base_dir / "skills"
        if not skills_dir.exists():
            return []
        skills: list[dict[str, Any]] = []
        seen: set[str] = set()
        for skill_md in sorted(skills_dir.rglob("SKILL.md")):
            try:
                text = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue
            info = self._extract_skill_frontmatter(text, skill_md.parent.name)
            if info["name"] in seen:
                continue
            seen.add(info["name"])
            location = str(skill_md.relative_to(self._base_dir))
            skills.append(
                {
                    "name": info["name"],
                    "description": info["description"],
                    "location": location,
                    "system_prompt_source": "/skills/",
                    "in_system_prompt": True,
                    "href": f"/skills?skill={info['name']}",
                }
            )
        return skills

    def _runtime_inventory(
        self,
        *,
        tools: list[Any],
        middleware: list[Any],
        skills: list[str],
        checkpointer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "middleware": self._middleware_inventory(middleware, skills),
            "tools": self._tool_inventory(tools),
            "skills": self._skills_inventory(),
            "subagents": self._subagent_inventory(),
            "package_versions": self._package_versions(),
            "checkpointer": checkpointer or {},
        }

    @staticmethod
    def _package_versions() -> dict[str, str]:
        packages = {
            "deepagents": "deepagents",
            "langchain": "langchain",
            "langchain_core": "langchain-core",
            "langgraph": "langgraph",
        }
        versions: dict[str, str] = {}
        for key, package_name in packages.items():
            try:
                versions[key] = version(package_name)
            except PackageNotFoundError:
                versions[key] = "not-installed"
        return versions

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @classmethod
    def _can_read_local_image_path(
        cls,
        path: Path,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> bool:
        workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
        if workspace is not None and cls._is_relative_to(path, workspace):
            return True
        if is_managed_resource_path(path, Path(__file__).resolve().parent.parent):
            return True
        if not workspace:
            return True
        if not session_id:
            return False
        try:
            return session_manager.has_external_file_read_permission(session_id, path)
        except AssertionError:
            return False

    @classmethod
    def _image_content_from_path(
        cls,
        path_text: str,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Convert an explicitly provided local image path into an OpenAI image_url block."""

        raw = path_text.replace("\\ ", " ").strip().strip("'\"")
        path = Path(raw).expanduser().resolve()
        try:
            if not path.is_file():
                return None
            mime = IMAGE_MIME_BY_EXT.get(path.suffix.lower())
            if not mime:
                return None
            if not cls._can_read_local_image_path(path, session_id=session_id, workspace_path=workspace_path):
                return None
            # Keep inline images bounded; very large screenshots should be
            # added through project files/tools instead of bloating a model call.
            if path.stat().st_size > 8 * 1024 * 1024:
                return None
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        except Exception:
            logger.warning("Failed to read image path for Agent input: %s", path_text, exc_info=True)
            return None

    @staticmethod
    def _image_content_from_attachment(
        attachment: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        attachment_id = str(attachment.get("id") or "").strip()
        if not attachment_id or not session_id:
            return None
        item = attachment_store.get(session_id, attachment_id)
        if not item or item.get("type") != "image":
            return None
        path = Path(str(item.get("path") or ""))
        try:
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                return None
            mime = str(item.get("mime_type") or IMAGE_MIME_BY_EXT.get(path.suffix.lower()) or "image/png")
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        except Exception:
            logger.warning("Failed to read attachment image for Agent input: %s", attachment_id, exc_info=True)
            return None

    @classmethod
    def _collect_image_inputs(
        cls,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """Collect authorized image payloads for the native task-launched image subagent."""

        inputs: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for attachment in attachments or []:
            if attachment.get("type") != "image":
                continue
            block = cls._image_content_from_attachment(attachment, session_id=session_id)
            url = str((block or {}).get("image_url", {}).get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            inputs.append(
                {
                    "ref": f"image_{len(inputs) + 1}",
                    "id": str(attachment.get("id") or ""),
                    "name": str(attachment.get("name") or f"image_{len(inputs) + 1}"),
                    "url": url,
                    "source": "attachment",
                }
            )

        for match in IMAGE_PATH_RE.finditer(message):
            raw_path = match.group("path")
            block = cls._image_content_from_path(raw_path, session_id=session_id, workspace_path=workspace_path)
            if not block:
                continue
            url = str(block.get("image_url", {}).get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            inputs.append(
                {
                    "ref": f"image_{len(inputs) + 1}",
                    "name": Path(raw_path.replace("\\ ", " ").strip().strip("'\"")).name,
                    "path": raw_path,
                    "url": url,
                    "source": "local_path",
                }
            )
        return inputs

    @classmethod
    def _build_user_content(
        cls,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
        allow_multimodal: bool = False,
    ) -> str | list[dict[str, Any]]:
        """Build user content.

        Main-agent calls default to text-only so providers that do not support
        OpenAI multimodal content parts never receive `image_url` blocks. The
        Harness image analyzer explicitly opts in with `allow_multimodal=True`.
        """

        if not allow_multimodal:
            attachment_refs = [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or item.get("path") or item.get("id") or "attachment"),
                    "type": str(item.get("type") or "file"),
                }
                for item in attachments or []
            ]
            image_names = [
                str(item.get("name") or item.get("path") or "image")
                for item in attachments or []
                if item.get("type") == "image"
            ]
            image_paths = [match.group("path") for match in IMAGE_PATH_RE.finditer(message)]
            local_resource_paths = [match.group("path") for match in LOCAL_RESOURCE_PATH_RE.finditer(message)]
            non_image_resource_paths = [path for path in local_resource_paths if path not in set(image_paths)]
            external_paths_needing_permission: list[str] = []
            external_resource_paths: list[str] = []
            workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
            for raw_path in local_resource_paths:
                path = Path(raw_path.replace("\\ ", " ").strip().strip("'\"")).expanduser().resolve()
                if (
                    workspace is not None
                    and not cls._is_relative_to(path, workspace)
                    and not cls._can_read_local_image_path(path, session_id=session_id, workspace_path=workspace)
                ):
                    external_resource_paths.append(str(path))
                    if raw_path in image_paths:
                        external_paths_needing_permission.append(str(path))

            if not attachment_refs and not local_resource_paths:
                return message

            notes = [
                    "[系统提示] 检测到附件输入。主 Agent 请求保持纯文本，不会直接接收文件 bytes/base64。"
                    "文本、Markdown、CSV、JSON 等非图片附件请调用 read_resource 读取 att_xxx；"
                    "图片附件请通过原生 task 子代理处理，并在 task description 中"
                    "原样保留下面的 harness_attachment_session_id 与 attachment refs。"
            ]
            if session_id:
                notes.append(f"[harness_attachment_session_id: {session_id}]")
            if attachment_refs:
                notes.append(
                    "[attachment refs]\n"
                    + "\n".join(
                        f"- {item['id'] or f'attachment_{index + 1}'}: {item['name']} ({item['type']})"
                        for index, item in enumerate(attachment_refs)
                    )
                )
            if image_names:
                notes.append("图片附件请优先调用 subagent_type=image_analyzer。")
            if image_paths:
                notes.append("[本地图片路径]\n" + "\n".join(f"- {path}" for path in image_paths))
            if non_image_resource_paths:
                notes.append(
                    "[本地文件路径]\n"
                    + "\n".join(f"- {path}" for path in non_image_resource_paths)
                    + "\n以上非 workspace 本地路径请调用 read_resource(resource=原始路径) 读取；不要调用 read_file、glob 或 grep。"
                )
            if external_resource_paths and not external_paths_needing_permission:
                paths = "\n".join(f"- {path}" for path in external_resource_paths)
                notes.append(
                    "[外部文件授权] 检测到 workspace 外的本地文件路径。主 Agent 必须通过 "
                    f"read_resource 触发授权并读取：\n{paths}"
                )
            if external_paths_needing_permission:
                paths = "\n".join(f"- {path}" for path in external_paths_needing_permission)
                notes.append(
                    "[外部文件授权] 检测到 workspace 外的本地图片路径。若尚未完成授权，主 Agent 必须先通过 "
                    f"read_resource 触发授权后再分析：\n{paths}"
                )
            return f"{message}\n\n" + "\n\n".join(notes)

        content: list[dict[str, Any]] = [{"type": "text", "text": message}]
        seen_urls: set[str] = set()
        external_paths_needing_permission: list[str] = []
        for attachment in attachments or []:
            if attachment.get("type") != "image":
                continue
            block = cls._image_content_from_attachment(attachment, session_id=session_id)
            url = str((block or {}).get("image_url", {}).get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            content.append({"type": "image_url", "image_url": {"url": url}})

        for match in IMAGE_PATH_RE.finditer(message):
            raw_path = match.group("path")
            block = cls._image_content_from_path(raw_path, session_id=session_id, workspace_path=workspace_path)
            if block:
                url = block["image_url"]["url"]
                if url not in seen_urls:
                    seen_urls.add(url)
                    content.append(block)
                continue
            path = Path(raw_path.replace("\\ ", " ").strip().strip("'\"")).expanduser().resolve()
            workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
            if (
                workspace is not None
                and not cls._is_relative_to(path, workspace)
                and not cls._can_read_local_image_path(path, session_id=session_id, workspace_path=workspace)
            ):
                external_paths_needing_permission.append(str(path))

        if external_paths_needing_permission:
            paths = "\n".join(f"- {path}" for path in external_paths_needing_permission)
            content[0]["text"] = (
                f"{message}\n\n"
                "[系统提示] 检测到 workspace 外且不属于 PuddingClaw 托管资源的本地图片路径，"
                "当前不会直接读取或内联给子代理。"
                "只有这种未授权外部图片，才需要主 Agent 先通过 read_resource 触发外部文件授权；"
                "授权完成后再重试该图片分析。\n"
                f"{paths}"
            )

        if len(content) == 1:
            return content[0]["text"]
        return content

    @staticmethod
    def _display_message_with_attachments(message: str, attachments: list[dict[str, Any]] | None = None) -> str:
        attachment_names = [
            str(item.get("name") or item.get("path") or item.get("id") or "attachment")
            for item in attachments or []
        ]
        if not attachment_names:
            return message
        suffix = "\n\n[附件]\n" + "\n".join(f"- {name}" for name in attachment_names)
        return f"{message}{suffix}"

    @classmethod
    def _build_messages(
        cls,
        history: list[dict[str, Any]],
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant", "system"} or content is None:
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
        messages.append({
            "role": "user",
            "content": cls._build_user_content(
                message,
                attachments,
                session_id=session_id,
                workspace_path=workspace_path,
            ),
        })
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
    def _extract_permission_interrupt(item: Any) -> dict[str, Any] | None:
        payload = item[1] if isinstance(item, tuple) and len(item) == 2 else item
        if not isinstance(payload, dict):
            return None
        interrupts = payload.get("__interrupt__")
        if not interrupts:
            return None
        if not isinstance(interrupts, (list, tuple)):
            interrupts = [interrupts]
        for interrupt_item in interrupts:
            value = getattr(interrupt_item, "value", interrupt_item)
            if isinstance(value, dict) and value.get("type") == "permission_request":
                request = value.get("request")
                if isinstance(request, dict):
                    return request
        return None

    async def _astream_with_permission_resume(
        self,
        agent: Any,
        initial_input: Any,
        *,
        stream_mode: list[str],
        config: dict[str, Any],
        context: dict[str, Any],
        trace_collector: TraceCollector,
    ) -> AsyncGenerator[Any, None]:
        graph_input = initial_input
        while True:
            interrupted_request: dict[str, Any] | None = None
            async for item in agent.astream(
                graph_input,
                stream_mode=stream_mode,
                config=config,
                context=context,
            ):
                request = self._extract_permission_interrupt(item)
                if request is not None:
                    interrupted_request = request
                    yield self._sse("permission_required", request)
                    break
                yield item

            if interrupted_request is None:
                return

            request_id = str(interrupted_request.get("id") or "")
            decision = await permission_resume_registry.wait(request_id)
            trace_collector.add_custom_span(
                "permission.decision",
                {"request_id": request_id, "decision": decision},
                span_type="permission",
                metadata={
                    "harness": {
                        "mechanism": "permission",
                        "pillars": [{"name": "architectural_constraints", "role": "primary"}],
                    },
                    "permission": {
                        "request_id": request_id,
                        "type": interrupted_request.get("type"),
                        "decision": decision.get("type"),
                        "outcome": "approved" if decision.get("type") == "approve" else "rejected",
                    },
                },
            )
            yield self._sse(
                "permission_resolved",
                {"request_id": request_id, "decision": decision},
            )
            graph_input = Command(resume={"decisions": [decision]})

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
    def _build_segment_trace_spans(
        collector: TraceCollector,
        segments: list[dict[str, Any]],
        accumulated_reasoning: str,
    ) -> list[Any]:
        """Convert UI segments into trace spans under the collector root."""

        segment_spans: list[Any] = []
        for seg_idx, segment in enumerate(segments):
            content = segment.get("content", "")
            timeline = segment.get("timeline") or []
            has_payload = bool(content or timeline)
            if not has_payload:
                continue

            llm_span = TraceSpan(
                span_id=f"{collector.trace_id}-segment-{seg_idx}",
                parent_id=collector.root.id,
                span_type="llm",
                name=f"model.{seg_idx}",
                started_at=collector.started_at,
                input_data=None,
                metadata={"segment_index": seg_idx},
            )
            llm_span.output = content
            llm_span.completed_at = time.time()
            llm_span.status = "completed"

            for item in timeline:
                item_type = item.get("type")
                if item_type == "reasoning":
                    reasoning_span = TraceSpan(
                        span_id=f"{collector.trace_id}-segment-{seg_idx}-reasoning-{len(llm_span.children)}",
                        parent_id=llm_span.id,
                        span_type="reasoning",
                        name="reasoning",
                        started_at=collector.started_at,
                    )
                    reasoning_span.output = item.get("content", "")
                    reasoning_span.completed_at = time.time()
                    reasoning_span.status = "completed"
                    llm_span.children.append(reasoning_span)
                elif item_type == "tool":
                    tc = item.get("tool_call") or {}
                    tool_span = TraceSpan(
                        span_id=f"{collector.trace_id}-segment-{seg_idx}-tool-{len(llm_span.children)}",
                        parent_id=llm_span.id,
                        span_type=DeepAgentsManager._trace_type_for_tool(
                            str(tc.get("tool", "unknown_tool")),
                            str(tc.get("input", "")),
                        ),
                        name=tc.get("tool", "unknown_tool"),
                        started_at=collector.started_at,
                        input_data=tc.get("input"),
                        metadata={"tool_call_id": tc.get("id")},
                    )
                    tool_span.output = tc.get("output")
                    tool_span.completed_at = time.time()
                    tool_span.status = "error" if tc.get("is_error") else "completed"
                    llm_span.children.append(tool_span)

            segment_spans.append(llm_span)

        return segment_spans

    @staticmethod
    def _trace_type_for_tool(tool_name: str, tool_input: str = "") -> str:
        lower_name = tool_name.lower()
        lower_input = tool_input.lower()
        if lower_name in {"task", "delegate"} or "subagent" in lower_name:
            return "subagent"
        if lower_name == "execute_skill" or "skill" in lower_name:
            return "skill"
        if lower_name.startswith("save_") and lower_name.endswith("_memory"):
            return "memory"
        if lower_name in {"search_user_memories", "search_feedback_memories", "search_project_memories", "search_reference_memories"}:
            return "memory"
        if lower_name in {"read_file", "write_file"} and "memory" in lower_input:
            return "memory"
        if lower_name == "read_external_file":
            return "permission"
        if lower_name == "read_resource" and "att_" not in str(tool_input):
            return "permission"
        return "tool"

    @staticmethod
    def _tool_harness_metadata(span_type: str) -> dict[str, Any]:
        mechanism_by_type = {
            "skill": "tool_use",
            "memory": "context_management",
            "subagent": "subagents",
            "tool": "tool_use",
            "permission": "permission",
        }
        pillars_by_type = {
            "memory": [
                {"name": "context_engineering", "role": "primary"},
                {"name": "garbage_collection", "role": "supporting"},
            ],
            "subagent": [
                {"name": "context_engineering", "role": "primary"},
                {"name": "architectural_constraints", "role": "supporting"},
                {"name": "garbage_collection", "role": "supporting"},
            ],
            "tool": [{"name": "architectural_constraints", "role": "primary"}],
            "permission": [{"name": "architectural_constraints", "role": "primary"}],
            "skill": [{"name": "architectural_constraints", "role": "primary"}],
        }
        return {
            "harness": {
                "mechanism": mechanism_by_type.get(span_type, "tool_use"),
                "pillars": pillars_by_type.get(
                    span_type,
                    [{"name": "architectural_constraints", "role": "primary"}],
                ),
            }
        }

    @staticmethod
    def _todo_diff(
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        before = {str(item.get("id", idx)): item for idx, item in enumerate(previous)}
        after = {str(item.get("id", idx)): item for idx, item in enumerate(current)}
        added = [after[key] for key in after.keys() - before.keys()]
        removed = [before[key] for key in before.keys() - after.keys()]
        updated: list[dict[str, Any]] = []
        for key in before.keys() & after.keys():
            if before[key] != after[key]:
                updated.append({"id": key, "before": before[key], "after": after[key]})
        return {"added": added, "updated": updated, "removed": removed}

    @staticmethod
    def _langsmith_callbacks() -> list[Any] | None:
        """Return LangSmith tracer callbacks only when explicitly enabled.

        Local white-box trace collection is always on via TraceCollector;
        LangSmith is an optional cloud complement controlled by env vars.
        """

        if os.environ.get("LANGSMITH_TRACING", "").lower() not in {"true", "1", "yes"}:
            return None
        if not os.environ.get("LANGSMITH_API_KEY"):
            return None
        try:
            from langchain.callbacks.tracers import LangChainTracer

            return [LangChainTracer()]
        except Exception:
            return None

    @staticmethod
    def _normalize_todos(raw_todos: list[Any]) -> list[dict[str, Any]]:
        """Normalize DeepAgents todo state into a plain JSON-serializable list."""

        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_todos):
            if not item:
                continue
            if isinstance(item, dict):
                todo = dict(item)
            else:
                # TodoListMiddleware may use dataclasses or pydantic models.
                todo = {
                    "content": getattr(item, "content", str(item)),
                    "status": getattr(item, "status", "pending"),
                }
            todo.setdefault("id", f"todo-{idx}")
            todo.setdefault("content", "")
            todo.setdefault("status", "pending")
            todo.setdefault("created_at", time.time())
            todo.setdefault("updated_at", time.time())
            normalized.append(todo)
        return normalized

    @staticmethod
    def _metadata_node(metadata: Any) -> str:
        if isinstance(metadata, dict):
            return str(metadata.get("langgraph_node") or "")
        return ""

    @staticmethod
    def _graph_structure(agent: Any) -> dict[str, Any] | None:
        """Extract LangGraph nodes and edges for frontend visualization.

        Falls back gracefully when the graph object is not available or uses
        an unexpected API shape.
        """
        try:
            get_graph = getattr(agent, "get_graph", lambda **_kwargs: None)
            try:
                graph = get_graph(xray=True)
            except TypeError:
                graph = get_graph()
            if graph is None:
                return None
            mermaid: str | None = None
            mermaid_png_data_url: str | None = None
            try:
                draw_mermaid = getattr(graph, "draw_mermaid", None)
                if callable(draw_mermaid):
                    mermaid = draw_mermaid()
            except Exception:
                logger.debug("Unable to draw LangGraph mermaid source", exc_info=True)
            try:
                draw_mermaid_png = getattr(graph, "draw_mermaid_png", None)
                if callable(draw_mermaid_png):
                    png_bytes = draw_mermaid_png(max_retries=1, retry_delay=0.2)
                    if isinstance(png_bytes, bytes) and png_bytes:
                        encoded = base64.b64encode(png_bytes).decode("ascii")
                        mermaid_png_data_url = f"data:image/png;base64,{encoded}"
            except Exception:
                logger.debug("Unable to draw LangGraph mermaid PNG", exc_info=True)
            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, Any]] = []
            for node in getattr(graph, "nodes", []) or []:
                node_id = ""
                node_type = "normal"
                node_data = node
                if isinstance(node, tuple):
                    node_id = str(node[0]) if node else ""
                    node_data = node[1] if len(node) > 1 else None
                elif isinstance(node, dict):
                    node_id = str(node.get("id", ""))
                    node_type = str(node.get("type", "normal"))
                else:
                    node_id = str(getattr(node, "id", getattr(node, "name", str(node))))
                    node_type = str(getattr(node, "type", "normal"))
                nodes.append({"id": node_id, "type": node_type, "data": node_data})
            for edge in getattr(graph, "edges", []) or []:
                if isinstance(edge, tuple):
                    if len(edge) >= 2:
                        edges.append({"source": str(edge[0]), "target": str(edge[1])})
                elif isinstance(edge, dict):
                    source = str(edge.get("source", ""))
                    target = str(edge.get("target", ""))
                    if source and target:
                        edges.append({"source": source, "target": target})
                else:
                    source = str(getattr(edge, "source", ""))
                    target = str(getattr(edge, "target", ""))
                    if source and target:
                        edges.append({"source": source, "target": target})
            result: dict[str, Any] = {"nodes": nodes, "edges": edges}
            if mermaid:
                result["mermaid"] = mermaid
            if mermaid_png_data_url:
                result["mermaid_png_data_url"] = mermaid_png_data_url
            return result
        except Exception:
            logger.debug("Unable to extract LangGraph structure", exc_info=True)
            return None

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
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, str], None]:
        """Stream Agent-mode SSE events compatible with the existing frontend."""

        query_id = f"query-{uuid.uuid4().hex[:12]}"
        trace_collector: TraceCollector | None = None
        trace_context_active = False
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
            messages = self._build_messages(
                history,
                message,
                attachments,
                session_id=session_id,
                workspace_path=workspace_path,
            )
            historical_tool_call_ids = {
                tc.get("id")
                for msg in messages
                for tc in msg.get("tool_calls") or []
                if tc.get("id")
            }

            # Restore persisted todos so the Agent resumes from white-box state
            # instead of relying on checkpoint black-box.
            persisted_todos = session_manager.get_todos(session_id)

            model = ModelClientChatModel(role="agent", streaming=True)
            agent_tools = self._build_tools(workspace_path, session_id=session_id)
            agent_skills = ["/skills/"]
            agent_middlewares = self._build_middlewares(project_id)
            checkpointer = await self._build_checkpointer()
            runtime_inventory = self._runtime_inventory(
                tools=agent_tools,
                skills=agent_skills,
                middleware=agent_middlewares,
                checkpointer=self._checkpointer_info,
            )
            traced_middlewares = wrap_middlewares_for_trace(agent_middlewares)
            logger.info("Building DeepAgents agent for session=%s project=%s", session_id, project_id)
            subagents = _build_subagents(agent_tools, agent_skills)
            system_prompt = build_deepagents_system_prompt(self._base_dir, workspace_path)
            agent = create_deep_agent(
                model=model,
                tools=agent_tools,
                skills=agent_skills,
                middleware=traced_middlewares,
                subagents=subagents,
                checkpointer=checkpointer,
                backend=self._build_backend(workspace_path),
                system_prompt=system_prompt,
            )
            logger.info("DeepAgents agent built successfully for session=%s", session_id)

            graph_structure = self._graph_structure(agent)
            if graph_structure:
                session_manager.update_graph(session_id, graph_structure)
                yield self._sse("graph_structure", graph_structure)

            emitted_text = ""
            final_state: dict[str, Any] | None = None
            tools_just_finished = False
            emitted_tool_starts: set[str] = set()
            pending_tool_starts: dict[str, dict[str, str]] = {}
            turn_sources: list[dict[str, Any]] = []
            # Buffer trace events emitted synchronously by TraceCollector so they
            # can be yielded asynchronously through the SSE stream.
            pending_trace_events: list[dict[str, str]] = []

            def _trace_emit(event: str, payload: dict[str, Any]) -> None:
                pending_trace_events.append(self._sse(event, payload))

            trace_collector = TraceCollector(
                session_id=session_id,
                query_id=query_id,
                emit_callback=_trace_emit,
                runtime_inventory=runtime_inventory,
            )
            trace_collector.__enter__()
            trace_context_active = True
            active_llm_span: str | None = None
            model_call_index = 0
            emitted_graph_structure: bool = False
            active_graph_node: str | None = None

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
            # Track todo state across stream values for white-box persistence.
            previous_todos: list[dict[str, Any]] = list(persisted_todos)

            agent_config: dict[str, Any] = {
                "configurable": {"thread_id": session_id, "user_id": user_id}
            }
            langsmith_callbacks = self._langsmith_callbacks()
            if langsmith_callbacks:
                agent_config["callbacks"] = langsmith_callbacks

            initial_state: dict[str, Any] = {"messages": messages, "todos": persisted_todos}

            async for item in self._astream_with_permission_resume(
                agent,
                initial_state,
                stream_mode=["messages", "updates", "custom", "values"],
                config=agent_config,
                context={"session_id": session_id, "query_id": query_id, "user_id": user_id},
                trace_collector=trace_collector,
            ):
                if isinstance(item, dict) and "event" in item and "data" in item:
                    yield item
                    continue
                # Drain any trace events emitted synchronously by the collector
                # (span_start/span_end) before processing the next stream item.
                while pending_trace_events:
                    yield pending_trace_events.pop(0)

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

                    # Track active LangGraph node for frontend graph highlight.
                    node = self._metadata_node(metadata)
                    if node and node != active_graph_node:
                        active_graph_node = node
                        trace_collector.add_graph_node_span(node)
                        yield self._sse(
                            "graph_node_active",
                            {"node": node, "trace_id": trace_collector.trace_id},
                        )

                    # Lifecycle of an LLM span: start on the first model or
                    # reasoning chunk, finish when the model node changes or a
                    # tool-call follows.
                    is_model_node = not node or node == "model"
                    if is_model_node and (text or reasoning_text):
                        if active_llm_span is None:
                            current_model_call_index = model_call_index
                            model_call_index += 1
                            active_llm_span = trace_collector.start_llm_span(
                                name=f"model.{current_model_call_index}",
                                input_data=None,
                                metadata={
                                    "graph_node": node or "model",
                                    "model_call_index": current_model_call_index,
                                },
                            )
                    elif active_llm_span is not None and not is_model_node:
                        trace_collector.finish_llm_span(output=emitted_text)
                        active_llm_span = None

                    if reasoning_text and thinking_enabled:
                        if not node or node == "model":
                            emitted_reasoning = True
                            accumulated_reasoning += reasoning_text
                            trace_collector.add_reasoning_span(reasoning_text)
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
                            # Close the current LLM span before handing off to tools.
                            if active_llm_span is not None:
                                trace_collector.finish_llm_span(output=emitted_text)
                                active_llm_span = None
                            continue
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
                            if active_llm_span is not None:
                                trace_collector.finish_llm_span(output=emitted_text)
                                active_llm_span = None
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
                                # Close any running LLM span before recording tool execution.
                                if active_llm_span is not None:
                                    trace_collector.finish_llm_span(output=emitted_text)
                                    active_llm_span = None
                                trace_collector.finish_tool_span(
                                    tool_call_id=tc_id or "",
                                    output=raw_output,
                                    is_error=is_error,
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
                                    span_type = self._trace_type_for_tool(tool_name, tool_input)
                                    tool_metadata = {
                                        "graph_node": node_name,
                                        **self._tool_harness_metadata(span_type),
                                    }
                                    trace_collector.start_tool_span(
                                        name=tool_name,
                                        tool_call_id=tc_id or "",
                                        input_data=tool_input,
                                        span_type=span_type,
                                        metadata=tool_metadata,
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
                        trace_collector.add_custom_span(event_type, payload)
                        yield self._sse(event_type, payload)
                elif mode == "values" and isinstance(payload, dict):
                    final_state = payload
                    # White-box todo persistence: TodoListMiddleware injects a
                    # `todos` field into graph state. Sync it to session.json and
                    # notify the frontend whenever it changes.
                    current_todos = payload.get("todos")
                    if isinstance(current_todos, list) and current_todos != previous_todos:
                        normalized = self._normalize_todos(current_todos)
                        diff = self._todo_diff(previous_todos, normalized)
                        now = time.time()
                        changed_ids = {
                            str(item.get("id"))
                            for item in diff["added"]
                            if item.get("id") is not None
                        }
                        changed_ids.update(
                            str(item.get("id"))
                            for item in diff["removed"]
                            if item.get("id") is not None
                        )
                        changed_ids.update(str(item.get("id")) for item in diff["updated"])
                        for todo in normalized:
                            if str(todo.get("id")) in changed_ids:
                                todo["last_changed_query_id"] = query_id
                                todo["updated_at"] = now
                        session_manager.update_todos(session_id, normalized)
                        previous_todos = list(normalized)
                        trace_collector.add_todo_span(normalized, diff=diff)
                        yield self._sse(
                            "todos_updated",
                            {"todos": normalized, "session_id": session_id, "query_id": query_id},
                        )

            # Close any still-running LLM span at the end of the stream.
            if active_llm_span is not None:
                trace_collector.finish_llm_span(output=emitted_text)
                active_llm_span = None

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
                trace_collector.finish_tool_span(
                    tool_call_id=tc_id,
                    output=failed_output,
                    is_error=True,
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

            # Drain any remaining synchronous trace events before building the final trace.
            while pending_trace_events:
                yield pending_trace_events.pop(0)

            session_manager.save_message(session_id, "user", self._display_message_with_attachments(message, attachments))
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

            # Streaming already emitted model/tool/reasoning spans. Avoid
            # rebuilding segment spans here because that duplicates simple
            # turns such as "你好" as two model calls.
            trace = trace_collector.finish(status="completed")
            session_manager.update_trace(session_id, trace, query_id=query_id)
            yield self._sse(
                "trace_updated",
                {"trace": trace, "session_id": session_id, "query_id": query_id},
            )
            if trace_context_active:
                trace_collector.__exit__(None, None, None)
                trace_context_active = False

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
            try:
                if trace_collector is not None:
                    trace = trace_collector.finish(status="error", error=error_msg)
                    session_manager.update_trace(session_id, trace, query_id=query_id)
            except Exception:
                pass
            if trace_context_active and trace_collector is not None:
                trace_collector.__exit__(type(exc), exc, exc.__traceback__)
            yield self._sse("error", {"error": error_msg, "message": error_msg})


deepagents_agent_manager = DeepAgentsAgentManager()
