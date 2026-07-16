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
