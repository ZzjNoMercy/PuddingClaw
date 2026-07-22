from __future__ import annotations

import hashlib
from types import SimpleNamespace

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


def test_authorized_external_directory_glob_is_snapshotted_and_rewritten(tmp_path):
    from deepagents.backends import CompositeBackend, FilesystemBackend

    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
    from graph.session_manager import session_manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "report.html").write_text("<h1>ok</h1>", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
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
    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={"/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True)},
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
    assert captured["args"]["path"].startswith("/scratch/external-directories/directory-search-")
    staged = scratch / captured["args"]["path"].removeprefix("/scratch/") / "report.html"
    assert staged.read_text(encoding="utf-8") == "<h1>ok</h1>"


def test_authorized_exact_external_file_grep_does_not_expand_parent(tmp_path):
    from deepagents.backends import CompositeBackend, FilesystemBackend

    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
    from graph.session_manager import session_manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target = external / "report.html"
    target.write_text("needle", encoding="utf-8")
    (external / "secret.txt").write_text("must-not-stage", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
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
    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={"/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True)},
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
    assert routed.startswith("/scratch/external-search-files/")
    staged = scratch / routed.removeprefix("/scratch/")
    assert staged.read_text(encoding="utf-8") == "needle"
    assert not any(path.name == "secret.txt" for path in scratch.rglob("*"))
    leases = session_manager.list_external_artifact_leases("routing-session")
    assert len(leases) == 1
    assert leases[0]["status"] == "search_snapshot"
    assert leases[0]["search_only"] is True
    assert leases[0]["run_id"] == "run-routing"
    assert leases[0]["query_id"] == "query-routing"
    assert leases[0]["staged_path"] == routed

    target.write_text("changed after immutable Run snapshot", encoding="utf-8")
    captured.clear()
    second = WorkspacePathRouterMiddleware(backend).wrap_tool_call(
        _request(
            "grep",
            {"path": str(target), "pattern": "needle"},
            workspace,
            call_id="call-2",
        ),
        handler,
    )
    assert second.status == "success"
    assert captured["args"]["path"] == routed
    assert staged.read_text(encoding="utf-8") == "needle"
    assert len(session_manager.list_external_artifact_leases("routing-session")) == 1


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
    assert "original search will be replayed" in str(result.content)
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


def test_symlink_escape_is_classified_as_external(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("outside", encoding="utf-8")
    link = workspace / "linked.txt"
    link.symlink_to(external)

    kind, routed = WorkspacePathRouterMiddleware._classify_path(str(link), str(workspace))

    assert kind == "external"
    assert routed == str(external.resolve())
