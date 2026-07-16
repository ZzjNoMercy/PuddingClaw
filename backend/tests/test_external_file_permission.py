from __future__ import annotations

import asyncio


def test_session_external_file_permission_grants(tmp_path):
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("permission-session")

    external_file = tmp_path / "Downloads" / "note.md"
    external_file.parent.mkdir()
    external_file.write_text("hello", encoding="utf-8")

    assert not session_manager.has_external_file_read_permission("permission-session", external_file)

    grant = session_manager.add_permission_grant(
        "permission-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(external_file.resolve()),
        capabilities=["read", "external_path"],
    )

    assert session_manager.has_external_file_read_permission("permission-session", external_file)
    assert session_manager.revoke_permission_grant("permission-session", grant["id"])
    assert not session_manager.has_external_file_read_permission("permission-session", external_file)

    session_manager.add_permission_grant(
        "permission-session",
        grant_type="external_file_read",
        target_kind="all_external_files",
        target="*",
        capabilities=["read", "external_path"],
    )

    assert session_manager.has_external_file_read_permission("permission-session", external_file)


def test_session_external_file_write_grant_is_exact_file_only(tmp_path):
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("write-permission-session")
    first = tmp_path / "outside" / "first.txt"
    second = tmp_path / "outside" / "second.txt"
    first.parent.mkdir()
    first.write_text("before", encoding="utf-8")
    second.write_text("before", encoding="utf-8")

    grant = session_manager.add_permission_grant(
        "write-permission-session",
        grant_type="external_file_write",
        target_kind="exact_file",
        target=str(first.resolve()),
        capabilities=["write", "external_path"],
    )

    assert session_manager.has_external_file_write_permission("write-permission-session", first)
    assert not session_manager.has_external_file_write_permission("write-permission-session", second)
    assert not session_manager.has_external_file_read_permission("write-permission-session", first)
    assert session_manager.revoke_permission_grant("write-permission-session", grant["id"])
    assert not session_manager.has_external_file_write_permission("write-permission-session", first)


def test_permissioned_backend_edits_only_approved_external_file(tmp_path):
    from deepagents.backends import FilesystemBackend

    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("backend-write-session")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external" / "note.txt"
    external.parent.mkdir()
    external.write_text("before", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="backend-write-session",
    )

    denied = backend.edit(str(external), "before", "denied")
    assert denied.error
    assert external.read_text(encoding="utf-8") == "before"

    session_manager.add_permission_grant(
        "backend-write-session",
        grant_type="external_file_write",
        target_kind="exact_file",
        target=str(external.resolve()),
        capabilities=["write", "external_path"],
    )
    allowed = backend.edit(str(external), "before", "after")

    assert allowed.error is None
    assert allowed.path == str(external.resolve())
    assert external.read_text(encoding="utf-8") == "after"


def test_external_write_request_contains_change_preview(tmp_path):
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.permission_resume import permission_resume_registry

    async def create_request():
        return permission_resume_registry.create_external_file_request(
            session_id="preview-session",
            query_id="query-preview",
            tool_call_id="call-edit",
            path=tmp_path / "outside.txt",
            access="write",
            operation="edit_file",
            change_preview={"old_string": "before", "new_string": "after"},
        )

    request = asyncio.run(create_request())

    assert request["type"] == "external_file_write"
    assert request["capabilities"] == ["write", "external_path"]
    assert request["options"] == ["exact_file_session"]
    assert request["operation"] == "edit_file"
    assert request["change_preview"]["new_string"] == "after"
    assert ExternalFilePermissionMiddleware._external_write_path(
        str(tmp_path / "outside.txt"),
        str(tmp_path / "workspace"),
    ) == (tmp_path / "outside.txt").resolve()
    assert ExternalFilePermissionMiddleware._external_write_path(
        "/workspace/report.md",
        str(tmp_path / "workspace"),
    ) is None


def test_permission_middleware_interrupts_external_edit_file(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("middleware-write-session")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external" / "chart.js"
    external.parent.mkdir()
    external.write_text("before", encoding="utf-8")
    captured = {}

    def fake_interrupt(payload):
        captured.update(payload)
        return {"decisions": [{"type": "reject"}]}

    monkeypatch.setattr(permission_middleware_module, "interrupt", fake_interrupt)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "args": {
                            "file_path": str(external),
                            "old_string": "before",
                            "new_string": "after",
                        },
                        "id": "call-edit-external",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "middleware-write-session",
            "query_id": "query-write",
            "workspace_path": str(workspace),
        }
    )

    async def invoke():
        return ExternalFilePermissionMiddleware().after_model(state, runtime)

    assert asyncio.run(invoke()) is None
    request = captured["request"]
    assert captured["type"] == "permission_request"
    assert request["type"] == "external_file_write"
    assert request["path"] == str(external.resolve())
    assert request["tool_call_id"] == "call-edit-external"
    assert request["change_preview"] == {"old_string": "before", "new_string": "after"}


def test_permission_middleware_accepts_virtual_workspace_read_resource(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware

    def fail_interrupt(_payload):
        raise AssertionError("virtual workspace path must not request permission")

    monkeypatch.setattr(
        permission_middleware_module,
        "interrupt",
        fail_interrupt,
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_resource",
                        "args": {"resource": "/workspace/report.md"},
                        "id": "call-read-virtual",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "virtual-read-session",
            "query_id": "query-read",
            "workspace_path": str(tmp_path),
        }
    )

    assert ExternalFilePermissionMiddleware().after_model(state, runtime) is None


def test_read_resource_maps_virtual_workspace_path(tmp_path):
    from tools.read_resource_tool import ReadResourceTool

    report = tmp_path / "report.md"
    report.write_text("E2E_GOAL_OK", encoding="utf-8")
    tool = ReadResourceTool(
        session_id="virtual-read-session",
        workspace_path=str(tmp_path),
    )

    result = tool.invoke({"resource": "/workspace/report.md"})

    assert "E2E_GOAL_OK" in result
    assert "Permission required" not in result


def test_permission_middleware_interrupts_misrouted_external_read_file(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("middleware-read-route-session")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external" / "report.html"
    external.parent.mkdir()
    external.write_text("outside", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(
        permission_middleware_module,
        "interrupt",
        lambda payload: captured.update(payload),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "read_file",
                    "args": {"file_path": str(external), "offset": 100, "limit": 200},
                    "id": "call-read-external",
                    "type": "tool_call",
                }],
            )
        ]
    }
    runtime = SimpleNamespace(context={
        "session_id": "middleware-read-route-session",
        "query_id": "query-read-route",
        "workspace_path": str(workspace),
    })

    async def invoke():
        return ExternalFilePermissionMiddleware().after_model(state, runtime)

    assert asyncio.run(invoke()) is None
    request = captured["request"]
    assert request["type"] == "external_file_read"
    assert request["path"] == str(external.resolve())
    assert request["operation"] == "read_file"


def test_read_resource_supports_line_pagination(tmp_path):
    from graph.session_manager import session_manager
    from tools.read_resource_tool import ReadResourceTool

    session_manager.initialize(tmp_path)
    session_manager.create_session("resource-pagination-session")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external" / "large.html"
    external.parent.mkdir()
    external.write_text("\n".join(f"line-{index}" for index in range(20)), encoding="utf-8")
    session_manager.add_permission_grant(
        "resource-pagination-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(external.resolve()),
        capabilities=["read", "external_path"],
    )

    content = ReadResourceTool(
        session_id="resource-pagination-session",
        workspace_path=str(workspace),
    ).invoke({"resource": str(external), "offset": 5, "limit": 3})

    assert content.startswith("line-5\nline-6\nline-7")
    assert "Continue with offset=8" in content


def test_read_external_file_tool_requires_permission_and_records_trace(tmp_path):
    from graph.session_manager import session_manager
    from graph.trace_collector import TraceCollector
    from tools.read_external_file_tool import ReadExternalFileTool

    session_manager.initialize(tmp_path)
    session_manager.create_session("permission-tool-session")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_file = tmp_path / "Downloads" / "article.md"
    external_file.parent.mkdir()
    external_file.write_text("outside workspace", encoding="utf-8")

    tool = ReadExternalFileTool(
        session_id="permission-tool-session",
        workspace_path=str(workspace),
    )

    with TraceCollector(session_id="permission-tool-session", query_id="query-test") as trace:
        denied = tool.invoke({"path": str(external_file)})

    assert "Permission required" in denied
    denied_trace = trace.finish()
    permission_spans = [span for span in denied_trace["spans"] if span["type"] == "permission"]
    assert permission_spans
    assert permission_spans[-1]["name"] == "permission.request"
    assert permission_spans[-1]["metadata"]["permission"]["outcome"] == "needs_user"

    session_manager.add_permission_grant(
        "permission-tool-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(external_file.resolve()),
        capabilities=["read", "external_path"],
    )

    with TraceCollector(session_id="permission-tool-session", query_id="query-test-2") as trace:
        content = tool.invoke({"path": str(external_file)})

    assert content == "outside workspace"
    allowed_trace = trace.finish()
    permission_spans = [span for span in allowed_trace["spans"] if span["type"] == "permission"]
    assert permission_spans[-1]["name"] == "permission.enforce"
    assert permission_spans[-1]["metadata"]["permission"]["outcome"] == "allowed"


def test_read_resource_tool_reads_attachment_and_keeps_external_permission(tmp_path):
    from io import BytesIO

    from graph.attachment_store import attachment_store
    from graph.session_manager import session_manager
    from tools.read_resource_tool import ReadResourceTool

    attachment_store.initialize(tmp_path)
    session_manager.initialize(tmp_path)
    session_manager.create_session("resource-session")

    attachment = attachment_store.save(
        session_id="resource-session",
        filename="notes.md",
        mime_type="text/markdown",
        source="paste",
        stream=BytesIO(b"# Notes\n\nhello attachment"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_file = tmp_path / "Downloads" / "article.md"
    external_file.parent.mkdir()
    external_file.write_text("outside workspace", encoding="utf-8")

    tool = ReadResourceTool(session_id="resource-session", workspace_path=str(workspace))

    attachment_content = tool.invoke({"resource": attachment["id"]})
    assert "hello attachment" in attachment_content

    denied = tool.invoke({"resource": str(external_file)})
    assert "Permission required" in denied


def test_deny_permission_request_resumes_pending_run():
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry

    loop = asyncio.new_event_loop()
    future = loop.create_future()
    permission_resume_registry._pending["perm-req-deny-test"] = future
    permission_resume_registry._requests["perm-req-deny-test"] = {
        "id": "perm-req-deny-test",
        "session_id": "permission-session",
        "status": "pending",
    }

    client = TestClient(app)
    response = client.post(
        "/api/sessions/permission-session/permissions/deny",
        json={
            "permission_request_id": "perm-req-deny-test",
            "message": "No thanks.",
        },
    )

    assert response.status_code == 200
    assert response.json()["resumed"] is True
    assert future.done()
    assert future.result() == {"type": "reject", "message": "No thanks."}
    assert permission_resume_registry.get("perm-req-deny-test")["decision"]["type"] == "reject"
    loop.close()


def test_deny_permission_request_rejects_cross_session_resolution():
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry

    loop = asyncio.new_event_loop()
    future = loop.create_future()
    request_id = "perm-req-cross-session"
    permission_resume_registry._pending[request_id] = future
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "session_id": "owner-session",
        "status": "pending",
    }

    response = TestClient(app).post(
        "/api/sessions/attacker-session/permissions/deny",
        json={"permission_request_id": request_id},
    )

    assert response.status_code == 400
    assert not future.done()
    permission_resume_registry.resolve(
        request_id,
        {"type": "reject", "message": "test cleanup"},
    )
    loop.close()


def test_tool_action_grant_cannot_be_replayed(tmp_path):
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("tool-action-session")
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    request_id = "perm-req-tool-action"
    permission_resume_registry._pending[request_id] = future
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "tool_action",
        "session_id": "tool-action-session",
        "status": "pending",
        "fingerprint": "sha256:test",
        "tool_name": "execute",
        "command": "python3 --version",
        "reason": "arbitrary_interpreter:python3",
        "risk": "high",
    }
    client = TestClient(app)

    first = client.post(
        "/api/sessions/tool-action-session/permissions/tool-actions",
        json={"permission_request_id": request_id, "scope": "once"},
    )
    replay = client.post(
        "/api/sessions/tool-action-session/permissions/tool-actions",
        json={"permission_request_id": request_id, "scope": "once"},
    )

    assert first.status_code == 200
    assert replay.status_code == 409
    grants = session_manager.list_permission_grants("tool-action-session")
    assert len(grants) == 1
    assert grants[0]["metadata"] == {
        "tool_name": "execute",
        "command": "python3 --version",
        "reason": "arbitrary_interpreter:python3",
        "risk": "high",
    }
    loop.close()


def test_permission_api_grants_exact_external_write_from_pending_request(tmp_path):
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("write-api-session")
    target = (tmp_path / "outside" / "report.html").resolve()
    target.parent.mkdir()
    target.write_text("before", encoding="utf-8")
    request_id = "perm-req-write-test"
    loop = asyncio.new_event_loop()
    permission_resume_registry._pending[request_id] = loop.create_future()
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "external_file_write",
        "session_id": "write-api-session",
        "path": str(target),
        "status": "pending",
    }

    client = TestClient(app)
    response = client.post(
        "/api/sessions/write-api-session/permissions/external-files",
        json={
            "target_kind": "exact_file",
            "path": str(target),
            "permission_request_id": request_id,
        },
    )

    assert response.status_code == 200
    grant = response.json()["grant"]
    assert grant["type"] == "external_file_write"
    assert grant["capabilities"] == ["write", "external_path"]
    assert session_manager.has_external_file_write_permission("write-api-session", target)

    broad_request_id = "perm-req-write-broad-test"
    permission_resume_registry._pending[broad_request_id] = loop.create_future()
    permission_resume_registry._requests[broad_request_id] = {
        "id": broad_request_id,
        "type": "external_file_write",
        "session_id": "write-api-session",
        "path": str(target),
        "status": "pending",
    }
    broad_response = client.post(
        "/api/sessions/write-api-session/permissions/external-files",
        json={
            "target_kind": "all_external_files",
            "permission_request_id": broad_request_id,
        },
    )
    permission_resume_registry.resolve(
        broad_request_id,
        {"type": "reject", "message": "test cleanup"},
    )
    loop.close()
    assert broad_response.status_code == 400
