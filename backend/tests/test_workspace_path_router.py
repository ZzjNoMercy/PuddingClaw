from __future__ import annotations

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


def test_external_glob_is_rejected_before_execution(tmp_path):
    from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()

    result = WorkspacePathRouterMiddleware().wrap_tool_call(
        _request("glob", {"path": str(external), "pattern": "*.html"}, workspace),
        lambda _request: (_ for _ in ()).throw(AssertionError("glob must not execute")),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "cannot search outside" in str(result.content)
    assert "read_resource" in str(result.content)


def test_external_ls_is_rejected_as_exact_file_scope(tmp_path):
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
    assert "exact-file scoped" in str(result.content)
    assert "stage_external_directory" in str(result.content)
    assert "selecting that directory as the project workspace" in str(result.content)


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
