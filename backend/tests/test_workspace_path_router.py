from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage


def _request(tool_name: str, args: dict, workspace, *, call_id: str = "call-1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": tool_name,
            "args": args,
            "id": call_id,
            "type": "tool_call",
        },
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(
            context={
                "session_id": "routing-session",
                "run_id": "run-routing",
                "query_id": "query-routing",
                "workspace_path": str(workspace),
            }
        ),
    )


def test_absolute_workspace_path_is_rewritten_before_read_file(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    target = workspace / "reports" / "dashboard.html"
    target.parent.mkdir(parents=True)
    target.write_text("ok", encoding="utf-8")
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="ok",
            name="read_file",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("read_file", {"file_path": str(target)}, workspace),
        handler,
    )

    assert result.status == "success"
    assert captured["args"]["file_path"] == "/workspace/reports/dashboard.html"


def test_tmp_alias_is_rewritten_to_run_scratch_before_read_file(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="current run text",
            name="read_file",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("read_file", {"file_path": "/tmp/renmai_prd.txt"}, workspace),
        handler,
    )

    assert result.status == "success"
    assert captured["args"]["file_path"] == "/scratch/tmp/renmai_prd.txt"


def test_tmp_read_uses_current_run_scratch_not_stale_host_file(tmp_path):
    from deepagents.backends import CompositeBackend, FilesystemBackend

    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "current-run-scratch"
    host_tmp = tmp_path / "host-tmp"
    workspace.mkdir()
    (scratch / "tmp").mkdir(parents=True)
    host_tmp.mkdir()
    (scratch / "tmp" / "renmai_prd.txt").write_text("current run", encoding="utf-8")
    (host_tmp / "renmai_prd.txt").write_text("stale session", encoding="utf-8")
    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={"/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True)},
    )

    def handler(request):
        path = request.tool_call["args"]["file_path"]
        result = backend.read(path)
        return ToolMessage(
            content=str((result.file_data or {}).get("content") or ""),
            name="read_file",
            tool_call_id=request.tool_call["id"],
            status="error" if result.error else "success",
        )

    result = WorkspacePathRouterMiddleware(backend).wrap_tool_call(
        _request("read_file", {"file_path": "/tmp/renmai_prd.txt"}, workspace),
        handler,
    )

    assert result.status == "success"
    assert result.content == "current run"


def test_tmp_and_scratch_tmp_backend_routes_share_one_physical_file(tmp_path):
    from deepagents.backends import FilesystemBackend

    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    run_tmp = scratch / "tmp"
    workspace.mkdir()
    run_tmp.mkdir(parents=True)
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    scratch_backend = FilesystemBackend(root_dir=scratch, virtual_mode=True)
    tmp_backend = FilesystemBackend(root_dir=run_tmp, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/scratch/": scratch_backend,
            "/tmp/": tmp_backend,
        },
        session_id="",
        workspace_root=workspace,
    )

    written = backend.write("/tmp/result.txt", "same file")
    read = backend.read("/scratch/tmp/result.txt")

    assert written.error is None
    assert read.error is None
    assert (run_tmp / "result.txt").read_text() == "same file"
    assert (read.file_data or {}).get("content") == "same file"


def test_kernel_smart_reads_exact_ordinary_host_text_without_grant(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "Downloads" / "notes.txt"
    external.parent.mkdir()
    external.write_text("ordinary host text", encoding="utf-8")

    class Backend:
        execution_mode = "kernel"

    result = WorkspacePathRouterMiddleware(
        Backend(),
        approval_mode="smart",
    ).wrap_tool_call(
        _request("read_file", {"file_path": str(external)}, workspace),
        lambda _request: (_ for _ in ()).throw(
            AssertionError("Smart exact host read must use the bounded resource reader")
        ),
    )

    assert result.status == "success"
    assert result.content == "ordinary host text"


def test_absolute_managed_knowledge_path_is_rewritten_before_read_file(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    knowledge = tmp_path / "knowledge"
    target = knowledge / "imported" / "note.md"
    target.parent.mkdir(parents=True)
    target.write_text("managed note", encoding="utf-8")
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="managed note",
            name="read_file",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware(
        managed_host_path_aliases={"/knowledge": knowledge},
    ).wrap_tool_call(
        _request("read_file", {"file_path": str(target)}, workspace),
        handler,
    )

    assert result.status == "success"
    assert captured["name"] == "read_file"
    assert captured["args"]["file_path"] == "/knowledge/imported/note.md"


def test_read_resource_for_managed_text_uses_backend_read_file_route(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    knowledge = tmp_path / "knowledge"
    target = knowledge / "imported" / "note.md"
    target.parent.mkdir(parents=True)
    target.write_text("managed note", encoding="utf-8")
    captured = {}

    class Backend:
        managed_host_path_aliases = {"/knowledge": knowledge}

        def read(self, path, *, offset, limit):
            captured.update({"path": path, "offset": offset, "limit": limit})
            return SimpleNamespace(
                error=None,
                file_data={"encoding": "utf-8", "content": "managed note"},
            )

    def unexpected_handler(_request):
        raise AssertionError("managed text must bypass read_resource")

    result = WorkspacePathRouterMiddleware(Backend()).wrap_tool_call(
        _request(
            "read_resource",
            {"resource": str(target), "offset": 3, "limit": 20},
            workspace,
        ),
        unexpected_handler,
    )

    assert result.status == "success"
    assert result.name == "read_file"
    assert result.content == "managed note"
    assert captured == {
        "path": "/knowledge/imported/note.md",
        "offset": 3,
        "limit": 20,
    }


def test_managed_read_eperm_is_not_reported_as_session_permission_gap(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()

    class Backend:
        managed_host_path_aliases = {"/knowledge": knowledge}

        def read(self, _path, *, offset, limit):
            del offset, limit
            return SimpleNamespace(
                error="[Errno 1] Operation not permitted: '/managed/note.md'",
                file_data=None,
            )

    result = WorkspacePathRouterMiddleware(Backend()).wrap_tool_call(
        _request(
            "read_resource",
            {"resource": "/knowledge/imported/note.md"},
            workspace,
        ),
        lambda _request: (_ for _ in ()).throw(AssertionError("managed read must use backend")),
    )

    assert result.status == "error"
    assert "managed_resource_unavailable" in str(result.content)
    assert "not a Session external-file permission gap" in str(result.content)
    assert "Do not retry with the physical host path" in str(result.content)


def test_managed_ls_eperm_is_normalized_before_model_sees_it(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def handler(request):
        return ToolMessage(
            content="Error: Listing aborted: [Errno 1] Operation not permitted",
            name="ls",
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("ls", {"path": "/knowledge/"}, workspace),
        handler,
    )

    assert result.status == "error"
    assert "managed_resource_unavailable" in str(result.content)
    assert "do not request HITL authorization" in str(result.content)


def test_successful_managed_read_with_permission_terms_is_not_normalized(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = "Handle `permission denied` without retrying operation not permitted."

    def handler(request):
        return ToolMessage(
            content=content,
            name="read_file",
            tool_call_id=request.tool_call["id"],
            status="success",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("read_file", {"file_path": "/skills/example/SKILL.md"}, workspace),
        handler,
    )

    assert result.status == "success"
    assert result.content == content
    assert "managed_resource_unavailable" not in str(result.content)


def test_absolute_workspace_path_is_rewritten_before_ls(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    target = workspace / "reports"
    target.mkdir(parents=True)
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="ok",
            name="ls",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("ls", {"path": str(target)}, workspace),
        handler,
    )

    assert result.status == "success"
    assert captured["args"]["path"] == "/workspace/reports"


def test_glob_without_path_searches_workspace_once_and_returns_canonical_path(tmp_path):
    from deepagents.backends import FilesystemBackend

    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "reports" / "dashboard.html"
    target.parent.mkdir()
    target.write_text("<h1>ok</h1>", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    host_prefix = f"{workspace.resolve().as_posix().rstrip('/')}/"
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            host_prefix: workspace_backend,
        },
        session_id="",
        workspace_root=workspace,
    )

    def handler(request):
        args = request.tool_call["args"]
        result = backend.glob(args["pattern"], path=args.get("path"))
        paths = [match["path"] for match in result.matches or []]
        return ToolMessage(
            content=str(paths),
            name="glob",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware(backend).wrap_tool_call(
        _request("glob", {"pattern": "**/dashboard.html"}, workspace),
        handler,
    )

    assert result.content == "['/workspace/reports/dashboard.html']"
    assert backend.read("/workspace/reports/dashboard.html").error is None


def test_mounted_backends_bypass_host_broker_for_builtin_file_tools(tmp_path):
    from deepagents.backends import FilesystemBackend

    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend

    workspace = tmp_path / "workspace"
    knowledge = tmp_path / "knowledge"
    workspace.mkdir()
    knowledge.mkdir()
    (workspace / "draft.md").write_text("before needle", encoding="utf-8")
    (knowledge / "note.md").write_text("managed needle", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/knowledge/": FilesystemBackend(root_dir=knowledge, virtual_mode=True),
        },
        session_id="mounted-session",
        workspace_root=workspace,
        managed_readonly_roots=(knowledge,),
    )

    class UnexpectedHostBroker:
        def __getattr__(self, name):
            raise AssertionError(f"mounted path must not call HostFileBroker.{name}")

    backend.host_file_broker = UnexpectedHostBroker()

    assert backend.read("/knowledge/note.md").error is None
    assert backend.ls("/knowledge").error is None
    assert backend.glob("*.md", path="/knowledge").error is None
    assert backend.grep("needle", path="/knowledge", glob="*.md").error is None

    assert backend.write("/workspace/new.md", "created").error is None
    assert backend.edit("/workspace/draft.md", "before", "after").error is None
    assert (workspace / "new.md").read_text(encoding="utf-8") == "created"
    assert (workspace / "draft.md").read_text(encoding="utf-8") == "after needle"

    assert "read-only" in backend.write("/knowledge/new.md", "blocked").error.lower()
    assert "read-only" in backend.edit("/knowledge/note.md", "managed", "changed").error.lower()

    async def verify_async_routes():
        assert (await backend.aread("/knowledge/note.md")).error is None
        assert (await backend.als("/knowledge")).error is None
        assert (await backend.aglob("*.md", path="/knowledge")).error is None
        assert (await backend.agrep("needle", path="/knowledge", glob="*.md")).error is None
        assert (await backend.awrite("/workspace/async.md", "async-created")).error is None
        assert (await backend.aedit("/workspace/draft.md", "after", "final")).error is None

    asyncio.run(verify_async_routes())
    assert (workspace / "async.md").read_text(encoding="utf-8") == "async-created"
    assert (workspace / "draft.md").read_text(encoding="utf-8") == "final needle"


def test_grep_without_path_defaults_to_workspace(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="[]",
            name="grep",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("grep", {"pattern": "needle", "glob": "*.md"}, workspace),
        handler,
    )

    assert result.status == "success"
    assert captured["args"] == {
        "pattern": "needle",
        "glob": "*.md",
        "path": "/workspace",
    }


def test_explicit_composite_root_search_is_narrowed_to_workspace(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed: list[dict] = []

    def handler(request):
        observed.append(dict(request.tool_call["args"]))
        return ToolMessage(
            content="[]",
            name=str(request.tool_call["name"]),
            tool_call_id=str(request.tool_call["id"]),
            status="success",
        )

    middleware = WorkspacePathRouterMiddleware()
    middleware.wrap_tool_call(
        _request("glob", {"pattern": "**/*.py", "path": "/"}, workspace),
        handler,
    )
    middleware.wrap_tool_call(
        _request("grep", {"pattern": "needle", "path": "/"}, workspace),
        handler,
    )

    assert observed == [
        {"pattern": "**/*.py", "path": "/workspace"},
        {"pattern": "needle", "path": "/workspace"},
    ]


def test_glob_virtual_namespace_pattern_routes_to_that_namespace(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="[]",
            name="glob",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request(
            "glob",
            {"pattern": "/semantic-assets/references/**/*.md"},
            workspace,
        ),
        handler,
    )

    assert result.status == "success"
    assert captured["args"] == {
        "pattern": "references/**/*.md",
        "path": "/semantic-assets",
    }


def test_glob_workspace_host_pattern_is_canonicalized(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="[]",
            name="glob",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request(
            "glob",
            {"pattern": f"{workspace.resolve().as_posix()}/reports/**/*.html"},
            workspace,
        ),
        handler,
    )

    assert result.status == "success"
    assert captured["args"] == {
        "pattern": "reports/**/*.html",
        "path": "/workspace",
    }


@pytest.mark.asyncio
async def test_async_glob_without_path_defaults_to_workspace(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}

    async def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="[]",
            name="glob",
            tool_call_id="call-1",
            status="success",
        )

    result = await WorkspacePathRouterMiddleware().awrap_tool_call(
        _request("glob", {"pattern": "**/*.html"}, workspace),
        handler,
    )

    assert result.status == "success"
    assert captured["args"] == {
        "pattern": "**/*.html",
        "path": "/workspace",
    }


def test_virtual_namespace_roots_are_not_misclassified_as_external(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed: list[str] = []

    def handler(request):
        observed.append(request.tool_call["args"]["path"])
        return ToolMessage(
            content="[]",
            name="ls",
            tool_call_id=str(request.tool_call["id"]),
            status="success",
        )

    roots = [
        "/workspace",
        "/knowledge",
        "/semantic-assets",
        "/sql-guardrails",
        "/analytics-models",
        "/skills",
        "/large_tool_results",
        "/scratch",
    ]
    middleware = WorkspacePathRouterMiddleware()
    for index, root in enumerate(roots):
        result = middleware.wrap_tool_call(
            _request("ls", {"path": root}, workspace, call_id=f"call-{index}"),
            handler,
        )
        assert result.status == "success"

    assert observed == roots


def test_terminal_scratch_reference_resolves_to_formal_target_or_not_durable(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
    from graph.session_manager import session_manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("routing-session")
    formal_target = tmp_path / "formal" / "report.html"
    formal_target.parent.mkdir()
    formal_target.write_text("report", encoding="utf-8")
    formal_sha = "sha256:" + hashlib.sha256(formal_target.read_bytes()).hexdigest()
    delivered = session_manager.register_delivered_artifact(
        "routing-session",
        target_path=str(formal_target),
        content_sha256=formal_sha,
        source_run_id="run-old",
        source_query_id="query-old",
    )
    session_manager.upsert_external_artifact_lease(
        "routing-session",
        {
            "lease_id": "artifact-lease-committed",
            "staged_path": "/scratch/external/artifact-lease-committed/report.html",
            "target_path": str(formal_target),
            "status": "committed",
            "committed_sha256": formal_sha,
            "delivered_artifact_id": delivered["artifact_id"],
        },
    )
    session_manager.upsert_external_artifact_lease(
        "routing-session",
        {
            "lease_id": "artifact-lease-abandoned",
            "staged_path": "/scratch/external/artifact-lease-abandoned/report.html",
            "target_path": str(tmp_path / "formal" / "other.html"),
            "status": "abandoned",
        },
    )
    middleware = WorkspacePathRouterMiddleware()

    def never(_request):
        raise AssertionError("terminal scratch path must not execute")

    durable = middleware.wrap_tool_call(
        _request(
            "read_file",
            {"file_path": "/scratch/external/artifact-lease-committed/report.html"},
            workspace,
        ),
        never,
    )
    abandoned = middleware.wrap_tool_call(
        _request(
            "read_file",
            {"file_path": "/scratch/external/artifact-lease-abandoned/report.html"},
            workspace,
            call_id="call-2",
        ),
        never,
    )

    assert "terminal_scratch_ref" in durable.content
    assert str(tmp_path / "formal" / "report.html") in durable.content
    assert "artifact_not_durable" in abandoned.content


def test_external_read_file_routes_to_read_resource_with_pagination(tmp_path, monkeypatch):
    import graph.middlewares.workspace_path_router as module
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external" / "report.html"
    external.parent.mkdir()
    external.write_text("outside", encoding="utf-8")
    captured = {}

    def fake_invoke(_tool, args):
        captured.update(args)
        return "routed content"

    monkeypatch.setattr(module.ReadResourceTool, "invoke", fake_invoke)
    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request(
            "read_file",
            {"file_path": str(external), "offset": 100, "limit": 200},
            workspace,
        ),
        lambda _request: (_ for _ in ()).throw(AssertionError("read_file must not execute")),
    )

    assert isinstance(result, ToolMessage)
    assert result.name == "read_resource"
    assert result.status == "success"
    assert captured == {"resource": str(external.resolve()), "offset": 100, "limit": 200}


def test_spawn_external_reads_keep_direct_host_route_without_grant(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
    from graph.session_manager import session_manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "Downloads" / "notes.txt"
    external.parent.mkdir()
    external.write_text("spawn direct host read", encoding="utf-8")
    session_manager.initialize(tmp_path)
    session_manager.create_session("routing-session")

    class SpawnBackend:
        execution_mode = "spawn"
        managed_host_path_aliases = {}

    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="spawn direct host read",
            name="read_file",
            tool_call_id="call-1",
            status="success",
        )

    middleware = WorkspacePathRouterMiddleware(SpawnBackend())
    result = middleware.wrap_tool_call(
        _request("read_file", {"file_path": str(external)}, workspace),
        handler,
    )
    resource = middleware.wrap_tool_call(
        _request("read_resource", {"resource": str(external)}, workspace, call_id="call-2"),
        lambda _request: (_ for _ in ()).throw(AssertionError("spawn resource read is bound directly")),
    )

    assert result.status == "success"
    assert captured["args"]["file_path"] == str(external.resolve())
    assert resource.status == "success"
    assert "spawn direct host read" in str(resource.content)
    assert session_manager.list_permission_grants("routing-session") == []


def test_exact_external_read_resource_is_not_misclassified_as_directory_search(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    external = tmp_path / "external" / "product-config-charts-2024.js"
    workspace.mkdir()
    external.parent.mkdir()
    external.write_text("const data = {};", encoding="utf-8")
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(
            content="const data = {};",
            name="read_resource",
            tool_call_id="call-1",
            status="success",
        )

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request(
            "read_resource",
            {"resource": str(external), "offset": 200, "limit": 300},
            workspace,
        ),
        handler,
    )

    assert result.status == "success"
    assert captured["name"] == "read_resource"
    assert captured["args"] == {
        "resource": str(external),
        "offset": 200,
        "limit": 300,
    }


def test_authorized_external_directory_glob_keeps_canonical_host_path(tmp_path):
    from deepagents.backends import FilesystemBackend

    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "report.html").write_text("<h1>ok</h1>", encoding="utf-8")
    session_manager.initialize(tmp_path)
    session_manager.create_session("routing-session")
    session_manager.add_permission_grant(
        "routing-session",
        grant_type="external_directory_read",
        target_kind="exact_directory",
        target=str(external.resolve()),
        capabilities=["read", "recursive", "external_path"],
        scope="run",
        source="user",
        metadata={"run_id": "run-routing"},
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={"/workspace/": workspace_backend},
        session_id="routing-session",
        run_id="run-routing",
        query_id="query-routing",
        workspace_root=workspace,
    )
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(content="report.html", name="glob", tool_call_id="call-1")

    result = WorkspacePathRouterMiddleware(backend).wrap_tool_call(
        _request("glob", {"path": str(external), "pattern": "*.html"}, workspace),
        handler,
    )

    assert isinstance(result, ToolMessage)
    assert captured["args"]["path"] == str(external.resolve())
    assert session_manager.list_external_directory_leases("routing-session") == []


def test_authorized_exact_external_file_grep_keeps_exact_path_without_parent_access(tmp_path):
    from deepagents.backends import FilesystemBackend

    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target = external / "report.html"
    target.write_text("needle", encoding="utf-8")
    (external / "secret.txt").write_text("must-not-stage", encoding="utf-8")
    session_manager.initialize(tmp_path)
    session_manager.create_session("routing-session")
    session_manager.add_permission_grant(
        "routing-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(target.resolve()),
        capabilities=["read", "external_path"],
        scope="session",
        source="user",
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="routing-session",
        run_id="run-routing",
        query_id="query-routing",
        workspace_root=workspace,
    )
    captured = {}

    def handler(request):
        captured.update(request.tool_call)
        return ToolMessage(content="needle", name="grep", tool_call_id="call-1")

    result = WorkspacePathRouterMiddleware(backend).wrap_tool_call(
        _request("grep", {"path": str(target), "pattern": "needle"}, workspace),
        handler,
    )

    assert result.status == "success"
    routed = captured["args"]["path"]
    assert routed == str(target.resolve())
    assert backend.can_access_external_path(str(target), access="read") is True
    assert backend.can_access_external_path(str(external / "secret.txt"), access="read") is False
    assert session_manager.list_external_artifact_leases("routing-session") == []


def test_external_ls_without_directory_grant_returns_replayable_authorization_gap(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("ls", {"path": str(external)}, workspace),
        lambda _request: (_ for _ in ()).throw(AssertionError("ls must not execute")),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "explicit read authorization" in str(result.content)
    assert "replays it through HostFileBroker" in str(result.content)
    assert "Exact-file grants never expand" in str(result.content)


def test_scratch_is_virtual_and_misrouted_read_resource_uses_backend(tmp_path):
    from deepagents.backends import CompositeBackend, FilesystemBackend

    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    (scratch / "external" / "lease-1").mkdir(parents=True)
    (scratch / "external" / "lease-1" / "report.js").write_text(
        "const ok = true;",
        encoding="utf-8",
    )
    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={"/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True)},
    )
    result = WorkspacePathRouterMiddleware(backend).wrap_tool_call(
        _request(
            "read_resource",
            {"resource": "/scratch/external/lease-1/report.js"},
            workspace,
        ),
        lambda _request: (_ for _ in ()).throw(AssertionError("read_resource must be adapted")),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert result.name == "read_file"
    assert result.content == "const ok = true;"


def test_symlink_escape_is_rejected_by_shared_authority_classifier(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("outside", encoding="utf-8")
    link = workspace / "linked.txt"
    link.symlink_to(external)

    kind, routed = WorkspacePathRouterMiddleware._classify_path(str(link), str(workspace))

    assert kind == "escape"
    assert routed == str(external.resolve())

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("read_file", {"file_path": str(link)}, workspace),
        lambda _request: (_ for _ in ()).throw(
            AssertionError("workspace symlink escape must not execute")
        ),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "escapes its workspace" in str(result.content)
