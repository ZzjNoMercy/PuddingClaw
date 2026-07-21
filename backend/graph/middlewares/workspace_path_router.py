"""Deterministic filesystem tool routing at the Agent execution boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from tools.read_resource_tool import ReadResourceTool


class WorkspacePathRouterMiddleware(AgentMiddleware):
    """Normalize workspace paths and reroute external reads before execution."""

    _PATH_ARGS = {
        "read_file": "file_path",
        "ls": "path",
        "glob": "path",
        "grep": "path",
        "read_resource": "resource",
    }
    _VIRTUAL_PREFIXES = (
        "/workspace/",
        "/knowledge/",
        "/semantic-assets/",
        "/sql-guardrails/",
        "/analytics-models/",
        "/skills/",
        "/large_tool_results/",
        "/scratch/",
    )

    def __init__(self, backend: Any | None = None) -> None:
        self.backend = backend

    @staticmethod
    def _runtime_context(request: ToolCallRequest) -> dict[str, Any]:
        runtime = request.runtime
        context = runtime.context if runtime is not None else None
        return context if isinstance(context, dict) else {}

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @classmethod
    def _classify_path(
        cls,
        raw_path: str,
        workspace_path: str,
    ) -> tuple[str, str]:
        """Return (kind, routed_path): virtual/relative/workspace/external."""
        normalized = raw_path.strip().replace("\\", "/")
        if not normalized:
            return "relative", normalized
        if normalized.startswith(cls._VIRTUAL_PREFIXES):
            return "virtual", normalized

        requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            return "relative", raw_path

        resolved = requested.resolve()
        if workspace_path:
            workspace = Path(workspace_path).expanduser().resolve()
            if cls._is_relative_to(resolved, workspace):
                relative = resolved.relative_to(workspace).as_posix()
                return "workspace", f"/workspace/{relative}"
        return "external", str(resolved)

    @staticmethod
    def _tool_message(
        request: ToolCallRequest,
        content: str,
        *,
        name: str,
    ) -> ToolMessage:
        is_error = content.startswith(("❌", "🔒"))
        return ToolMessage(
            content=content,
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=name,
            status="error" if is_error else "success",
        )

    def _route_request(
        self,
        request: ToolCallRequest,
    ) -> tuple[str, ToolCallRequest | ToolMessage]:
        tool_name = str(request.tool_call.get("name") or "")
        path_arg = self._PATH_ARGS.get(tool_name)
        if path_arg is None:
            return "execute", request

        args = dict(request.tool_call.get("args") or {})
        raw_path = str(args.get(path_arg) or "")
        context = self._runtime_context(request)
        workspace_path = str(context.get("workspace_path") or "")
        kind, routed_path = self._classify_path(raw_path, workspace_path)

        # `/scratch` is a Backend/Docker virtual namespace, not a host path.
        # Models occasionally choose read_resource merely because it is not
        # under `/workspace`; adapt that mistake at the execution boundary so
        # a valid staged artifact does not appear to be missing.
        if (
            tool_name == "read_resource"
            and kind == "virtual"
            and routed_path.startswith("/scratch/")
        ):
            if self.backend is None:
                return "result", self._tool_message(
                    request,
                    "❌ `/scratch/...` is a Docker/Backend virtual path. Use read_file, not read_resource.",
                    name="read_resource",
                )
            return "backend_read", (request, routed_path, args)  # type: ignore[return-value]

        # Exact host-side resources remain the responsibility of
        # ReadResourceTool and ExternalFilePermissionMiddleware. Only scratch
        # needs adaptation above; treating every external read_resource call as
        # a directory search would incorrectly force directory staging.
        if tool_name == "read_resource":
            return "execute", request

        if kind == "workspace":
            args[path_arg] = routed_path
            tool_call = {**request.tool_call, "args": args}
            return "execute", request.override(tool_call=tool_call)

        if kind != "external":
            return "execute", request

        if tool_name != "read_file":
            return "result", self._tool_message(
                request,
                (
                    f"❌ `{tool_name}` cannot search outside the current workspace. "
                    "External authorization is exact-file scoped and does not grant access to its parent directory. "
                    f"Use read_resource(resource={routed_path!r}) only when this is the exact file path, "
                    "or call stage_external_directory(directory_path=<parent directory>) to request explicit "
                    "directory access when sibling discovery is genuinely required. For whole-project debugging, "
                    "prefer selecting that directory as the project workspace."
                ),
                name=tool_name,
            )

        resource_args: dict[str, Any] = {"resource": routed_path}
        if "offset" in args:
            resource_args["offset"] = args["offset"]
        if "limit" in args:
            resource_args["limit"] = args["limit"]
        resource_tool = ReadResourceTool(
            session_id=str(context.get("session_id") or ""),
            workspace_path=workspace_path,
        )
        return "resource", (request, resource_tool, resource_args)  # type: ignore[return-value]

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        action, routed = self._route_request(request)
        if action == "execute":
            return handler(routed)  # type: ignore[arg-type]
        if action == "result":
            return routed  # type: ignore[return-value]
        if action == "backend_read":
            original, routed_path, args = routed  # type: ignore[misc]
            result = self.backend.read(
                routed_path,
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 2000),
            )
            if result.error:
                content = f"❌ Error reading staged artifact: {result.error}"
            else:
                data = result.file_data or {}
                if data.get("encoding") != "utf-8":
                    content = f"❌ Staged artifact is not UTF-8 text: {routed_path}"
                else:
                    content = str(data.get("content") or "")
            return self._tool_message(original, content, name="read_file")
        original, resource_tool, resource_args = routed  # type: ignore[misc]
        content = str(resource_tool.invoke(resource_args))
        return self._tool_message(original, content, name="read_resource")

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        action, routed = self._route_request(request)
        if action == "execute":
            return await handler(routed)  # type: ignore[arg-type]
        if action == "result":
            return routed  # type: ignore[return-value]
        if action == "backend_read":
            original, routed_path, args = routed  # type: ignore[misc]
            result = await self.backend.aread(
                routed_path,
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 2000),
            )
            if result.error:
                content = f"❌ Error reading staged artifact: {result.error}"
            else:
                data = result.file_data or {}
                if data.get("encoding") != "utf-8":
                    content = f"❌ Staged artifact is not UTF-8 text: {routed_path}"
                else:
                    content = str(data.get("content") or "")
            return self._tool_message(original, content, name="read_file")
        original, resource_tool, resource_args = routed  # type: ignore[misc]
        content = str(await resource_tool.ainvoke(resource_args))
        return self._tool_message(original, content, name="read_resource")
