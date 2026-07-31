"""Deterministic filesystem tool routing at the Agent execution boundary."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from graph.middlewares.external_directory import (
    LEASE_TTL_SECONDS,
    MAX_DIRECTORY_BYTES,
    _manifest_digest,
    _scan_source_directory,
    _upload_snapshot,
)
from graph.session_manager import session_manager
from graph.trace_collector import get_current_trace_collector
from graph.virtual_paths import (
    MANAGED_VIRTUAL_NAMESPACE_ROOTS,
    VIRTUAL_NAMESPACE_ROOTS,
    PathAuthority,
    classify_path_authority,
)
from observability import emit_harness_metric
from tools.read_resource_tool import ReadResourceTool

logger = logging.getLogger(__name__)


class WorkspacePathRouterMiddleware(AgentMiddleware):
    """Normalize workspace paths and reroute external reads before execution."""

    _SEARCH_TOOLS = frozenset({"glob", "grep"})

    _PATH_ARGS = {
        "read_file": "file_path",
        "ls": "path",
        "glob": "path",
        "grep": "path",
        "read_resource": "resource",
    }

    def __init__(
        self,
        backend: Any | None = None,
        *,
        managed_host_path_aliases: dict[str, str | Path] | None = None,
    ) -> None:
        self.backend = backend
        aliases = managed_host_path_aliases
        if aliases is None and backend is not None:
            aliases = getattr(backend, "managed_host_path_aliases", None)
        self.managed_host_path_aliases = {
            str(virtual_root).rstrip("/"): Path(host_root).expanduser().resolve()
            for virtual_root, host_root in dict(aliases or {}).items()
            if str(virtual_root).rstrip("/") in MANAGED_VIRTUAL_NAMESPACE_ROOTS
        }

    @classmethod
    def _default_search_scope(
        cls,
        tool_name: str,
        args: dict[str, Any],
        path_arg: str,
        workspace_path: str,
    ) -> dict[str, Any]:
        """Give implicit searches one canonical project scope.

        DeepAgents' CompositeBackend interprets an omitted search path as
        "scan the default backend and every route".  PuddingClaw intentionally
        exposes the workspace through multiple input aliases, so that behavior
        scans the same files repeatedly and leaks those aliases back to the
        model.  Tool schemas, however, describe an omitted path as the current
        working directory.  Make that contract deterministic here: ordinary
        implicit searches mean ``/workspace``.

        A glob pattern may itself name a managed virtual namespace.  Preserve
        that explicit intent by splitting the namespace from the pattern and
        routing the search to that single backend.
        """
        requested_scope = str(args.get(path_arg) or "").strip()
        if tool_name not in cls._SEARCH_TOOLS or requested_scope not in {"", "/"}:
            return args

        normalized_args = dict(args)
        if tool_name == "glob":
            pattern = str(normalized_args.get("pattern") or "").replace("\\", "/")
            for root in VIRTUAL_NAMESPACE_ROOTS:
                if pattern.startswith(f"{root}/"):
                    normalized_args[path_arg] = root
                    normalized_args["pattern"] = pattern[len(root) :].lstrip("/") or "**/*"
                    return normalized_args
            if workspace_path:
                workspace_root = Path(workspace_path).expanduser().resolve().as_posix().rstrip("/")
                if pattern == workspace_root or pattern.startswith(f"{workspace_root}/"):
                    normalized_args[path_arg] = "/workspace"
                    normalized_args["pattern"] = pattern[len(workspace_root) :].lstrip("/") or "**/*"
                    return normalized_args

        normalized_args[path_arg] = "/workspace"
        return normalized_args

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
        classified = classify_path_authority(
            raw_path,
            workspace_root=workspace_path or None,
        )
        routed = (
            classified.virtual_path
            or str(classified.canonical_host_path or classified.normalized_path)
        )
        if classified.authority is PathAuthority.WORKSPACE:
            return "workspace", routed
        if classified.authority in {PathAuthority.SCRATCH, PathAuthority.MANAGED}:
            return "virtual", routed
        if classified.authority is PathAuthority.ESCAPE:
            return "escape", routed
        return "external", routed

    def _managed_virtual_path(self, raw_path: str) -> tuple[str | None, bool]:
        """Map a configured managed host path back to its model-visible mount.

        CompositeBackend routes expose targets such as ``/knowledge``. Users,
        file pickers, and persisted source metadata may still provide the
        physical source path. Treat both spellings as the same read-only
        authority without teaching the model host-specific directory layouts.
        The boolean reports a lexical-in-root symlink escape so callers can
        fail closed instead of silently treating it as an external file.
        """

        normalized = str(raw_path or "").strip()
        if not normalized or not self.managed_host_path_aliases:
            return None, False
        requested = Path(normalized).expanduser()
        if not requested.is_absolute():
            return None, False
        lexical = Path(requested.absolute())
        canonical = requested.resolve(strict=False)
        for virtual_root, host_root in self.managed_host_path_aliases.items():
            lexical_inside = self._is_relative_to(lexical, host_root)
            canonical_inside = self._is_relative_to(canonical, host_root)
            if lexical_inside and not canonical_inside:
                return None, True
            if not canonical_inside:
                continue
            relative = canonical.relative_to(host_root).as_posix()
            return (
                virtual_root
                if relative == "."
                else f"{virtual_root}/{relative}",
                False,
            )
        return None, False

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

    def _authorized_directory_root(
        self,
        *,
        session_id: str,
        run_id: str,
        requested: Path,
    ) -> Path | None:
        """Return the narrowest authorized exact directory containing path."""

        candidates: list[Path] = []
        for grant in session_manager.list_permission_grants(session_id):
            if grant.get("type") != "external_directory_read":
                continue
            target = grant.get("target")
            if not isinstance(target, str) or not target:
                continue
            root = Path(target).expanduser().resolve()
            if not self._is_relative_to(requested, root):
                continue
            if session_manager.has_external_directory_permission(
                session_id,
                root,
                access="read",
                run_id=run_id,
            ):
                candidates.append(root)
        return max(candidates, key=lambda item: len(item.parts)) if candidates else None

    def _stage_exact_search_file(
        self,
        *,
        context: dict[str, Any],
        requested: Path,
    ) -> tuple[str | None, str | None]:
        if self.backend is None:
            return None, "external search requires a Backend scratch namespace"
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        query_id = str(context.get("query_id") or "")
        now = time.time()
        for lease in session_manager.list_external_artifact_leases(session_id):
            if (
                lease.get("search_only")
                and lease.get("status") == "search_snapshot"
                and str(lease.get("run_id") or "") == run_id
                and str(lease.get("query_id") or "") == query_id
                and str(lease.get("target_path") or "") == str(requested)
                and float(lease.get("expires_at") or 0) >= now
            ):
                staged_path = str(lease.get("staged_path") or "")
                probe = self.backend.read(staged_path)
                if not probe.error:
                    return staged_path, None
        try:
            content = requested.read_bytes()
        except OSError as exc:
            return None, str(exc)
        if len(content) > MAX_DIRECTORY_BYTES:
            return None, f"external file exceeds {MAX_DIRECTORY_BYTES} byte search limit"
        digest = hashlib.sha256(content).hexdigest()
        seed = f"{session_id}:{run_id}:{query_id}:{requested}:{digest}"
        snapshot_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        staged_dir = f"/scratch/external-search-files/{snapshot_id}"
        upload_error = _upload_snapshot(self.backend, staged_dir, {requested.name: content})
        if upload_error is not None:
            return None, upload_error
        staged_path = f"{staged_dir}/{requested.name}"
        now = time.time()
        session_manager.upsert_external_artifact_lease(
            session_id,
            {
                "lease_id": f"file-search-{snapshot_id}",
                "session_id": session_id,
                "run_id": run_id,
                "query_id": query_id,
                "goal_id": str(context.get("goal_id") or ""),
                "goal_revision": context.get("goal_revision"),
                "target_path": str(requested),
                "staged_path": staged_path,
                "source_sha256": f"sha256:{digest}",
                "status": "search_snapshot",
                "search_only": True,
                "created_at": now,
                "expires_at": now + LEASE_TTL_SECONDS,
            },
        )
        return staged_path, None

    def _stage_directory_search(
        self,
        *,
        context: dict[str, Any],
        root: Path,
        requested: Path,
    ) -> tuple[str | None, str | None]:
        if self.backend is None:
            return None, "external search requires a Backend scratch namespace"
        started_at = time.monotonic()
        collector = get_current_trace_collector()
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        query_id = str(context.get("query_id") or "")
        now = time.time()
        relative = requested.relative_to(root).as_posix()
        for lease in session_manager.list_external_directory_leases(session_id):
            if (
                lease.get("search_only")
                and lease.get("status") == "search_snapshot"
                and str(lease.get("run_id") or "") == run_id
                and str(lease.get("query_id") or "") == query_id
                and str(lease.get("directory_path") or "") == str(root)
                and float(lease.get("expires_at") or 0) >= now
            ):
                staged_dir = str(lease.get("staged_dir") or "")
                probe = self.backend.ls(staged_dir)
                if not probe.error:
                    return (
                        staged_dir
                        if relative == "."
                        else f"{staged_dir}/{relative}",
                        None,
                    )
        if collector is not None:
            collector.add_custom_span(
                "external_directory_snapshot_started",
                {"directory_path": str(root), "route": "automatic_search_snapshot"},
                span_type="tool",
            )
        manifest, contents, skipped, error = _scan_source_directory(root, include_content=True)
        if error is not None:
            if collector is not None:
                collector.add_custom_span(
                    "external_directory_snapshot_failed",
                    {
                        "directory_path": str(root),
                        "error": error,
                        "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                    },
                    span_type="tool",
                )
            return None, error
        source_digest = _manifest_digest(manifest)
        seed = f"{session_id}:{run_id}:{query_id}:{root}:{source_digest}"
        lease_id = "directory-search-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        staged_dir = f"/scratch/external-directories/{lease_id}"
        upload_error = _upload_snapshot(self.backend, staged_dir, contents)
        if upload_error is not None:
            if collector is not None:
                collector.add_custom_span(
                    "external_directory_snapshot_failed",
                    {
                        "directory_path": str(root),
                        "error": upload_error,
                        "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                    },
                    span_type="tool",
                )
            return None, upload_error
        now = time.time()
        lease = {
            "lease_id": lease_id,
            "session_id": session_id,
            "run_id": run_id,
            "query_id": query_id,
            "goal_id": str(context.get("goal_id") or ""),
            "goal_revision": context.get("goal_revision"),
            "directory_path": str(root),
            "staged_dir": staged_dir,
            "source_manifest": manifest,
            "source_manifest_sha256": source_digest,
            "file_count": len(manifest),
            "total_bytes": sum(int(item["size"]) for item in manifest.values()),
            "skipped": skipped,
            "status": "search_snapshot",
            "search_only": True,
            "created_at": now,
            "expires_at": now + LEASE_TTL_SECONDS,
        }
        session_manager.upsert_external_directory_lease(session_id, lease)
        if collector is not None:
            collector.add_custom_span(
                "external_directory_snapshot_completed",
                {
                    "directory_path": str(root),
                    "staged_dir": staged_dir,
                    "source_manifest_sha256": source_digest,
                    "file_count": len(manifest),
                    "total_bytes": lease["total_bytes"],
                    "skipped_count": len(skipped),
                    "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                },
                span_type="tool",
            )
        return staged_dir if relative == "." else f"{staged_dir}/{relative}", None

    def _route_external_search(
        self,
        *,
        request: ToolCallRequest,
        tool_name: str,
        args: dict[str, Any],
        path_arg: str,
        routed_path: str,
        context: dict[str, Any],
    ) -> tuple[str, ToolCallRequest | ToolMessage]:
        session_id = str(context.get("session_id") or "")
        emit_harness_metric(
            logger,
            "external_search_route",
            session_id=session_id,
            route="permission_required",
            tool=tool_name,
        )
        return "result", self._tool_message(
            request,
            (
                f"🔒 `{tool_name}` requires explicit read authorization for the exact external "
                f"file or directory {routed_path!r}. Keep the original search call: Harness requests "
                "the narrow permission and replays it through HostFileBroker after approval. "
                "Exact-file grants never expand to a parent directory."
            ),
            name=tool_name,
        )

    def _route_request(
        self,
        request: ToolCallRequest,
    ) -> tuple[str, ToolCallRequest | ToolMessage]:
        tool_name = str(request.tool_call.get("name") or "")
        path_arg = self._PATH_ARGS.get(tool_name)
        if path_arg is None:
            return "execute", request

        context = self._runtime_context(request)
        workspace_path = str(context.get("workspace_path") or "")
        original_args = dict(request.tool_call.get("args") or {})
        args = self._default_search_scope(
            tool_name,
            original_args,
            path_arg,
            workspace_path,
        )
        if args != original_args:
            request = request.override(
                tool_call={**request.tool_call, "args": args}
            )
        raw_path = str(args.get(path_arg) or "")
        managed_virtual_path, managed_escape = self._managed_virtual_path(raw_path)
        if managed_escape:
            return "result", self._tool_message(
                request,
                (
                    "❌ Path escapes its managed read-only authority through a symlink: "
                    f"{raw_path!r}."
                ),
                name=tool_name,
            )
        if managed_virtual_path is not None:
            args[path_arg] = managed_virtual_path
            request = request.override(
                tool_call={**request.tool_call, "args": args}
            )
            raw_path = managed_virtual_path
        kind, routed_path = self._classify_path(raw_path, workspace_path)

        if kind == "escape":
            return "result", self._tool_message(
                request,
                (
                    "❌ Path escapes its workspace or scratch authority: "
                    f"{raw_path!r}. Use a canonical path inside `/workspace` or "
                    "an explicitly authorized external host path."
                ),
                name=tool_name,
            )

        if kind == "virtual" and routed_path.startswith("/scratch/"):
            recovery = session_manager.resolve_terminal_scratch_reference(
                str(context.get("session_id") or ""),
                routed_path,
            )
            if isinstance(recovery, dict):
                if recovery.get("status") == "durable":
                    return "result", self._tool_message(
                        request,
                        (
                            "terminal_scratch_ref: this execution path is no longer authoritative. "
                            f"Retry with the formal target {recovery.get('formal_target_path')!r}; "
                            f"content_sha256={recovery.get('content_sha256')}; "
                            f"delivered_artifact_id={recovery.get('delivered_artifact_id')}. "
                            "Normal external-file permission still applies."
                        ),
                        name=tool_name,
                    )
                if recovery.get("status") == "artifact_stale":
                    return "result", self._tool_message(
                        request,
                        (
                            "artifact_stale: the formal delivery registry no longer matches the target. "
                            f"formal_target_path={recovery.get('formal_target_path')!r}; "
                            f"reason={recovery.get('stale_reason')}. Re-inspect or re-stage that exact "
                            "formal target; do not reuse the old scratch hash."
                        ),
                        name=tool_name,
                    )
                return "result", self._tool_message(
                    request,
                    (
                        "artifact_not_durable: the referenced scratch lease ended without a formal commit. "
                        "Re-stage the known formal source; do not glob the machine or guess sibling paths. "
                        f"lease_id={recovery.get('lease_id')}; lease_status={recovery.get('lease_status')}"
                    ),
                    name=tool_name,
                )

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

        # Managed filesystem mounts are regular Backend files. If the model
        # chose read_resource only because the user supplied the physical host
        # spelling, normalize the call to read_file semantics at the execution
        # boundary. Images remain resources because they need the image marker
        # consumed by the visual-analysis path.
        if (
            tool_name == "read_resource"
            and kind == "virtual"
            and any(
                routed_path == root or routed_path.startswith(f"{root}/")
                for root in MANAGED_VIRTUAL_NAMESPACE_ROOTS
            )
            and Path(routed_path).suffix.lower() not in {
                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"
            }
        ):
            if self.backend is None:
                return "result", self._tool_message(
                    request,
                    "❌ Managed filesystem path requires the Agent Backend read_file route.",
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
            emit_harness_metric(
                logger,
                "external_search_route",
                session_id=str(context.get("session_id") or ""),
                route="workspace",
                tool=tool_name,
            )
            args[path_arg] = routed_path
            tool_call = {**request.tool_call, "args": args}
            return "execute", request.override(tool_call=tool_call)

        if kind != "external":
            return "execute", request

        # Session Workspace Roots are direct host-file capabilities. Once the
        # exact file or containing exact-directory grant is active, keep the
        # original canonical path and let PermissionedCompositeBackend's
        # HostFileBroker serve the native file tool. No snapshot/lease path is
        # exposed to the model.
        can_access = getattr(self.backend, "can_access_external_path", None)
        if callable(can_access) and can_access(routed_path, access="read"):
            emit_harness_metric(
                logger,
                "external_search_route",
                session_id=str(context.get("session_id") or ""),
                route="host_file_broker",
                tool=tool_name,
            )
            args[path_arg] = routed_path
            return "execute", request.override(
                tool_call={**request.tool_call, "args": args}
            )

        if tool_name != "read_file":
            return self._route_external_search(
                request=request,
                tool_name=tool_name,
                args=args,
                path_arg=path_arg,
                routed_path=routed_path,
                context=context,
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
        # Directory scanning and upload touch the host filesystem and may be
        # sizeable; keep deterministic routing off the async model event loop.
        action, routed = await asyncio.to_thread(self._route_request, request)
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
