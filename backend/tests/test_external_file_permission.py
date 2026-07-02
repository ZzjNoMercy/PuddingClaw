from __future__ import annotations

import asyncio
from pathlib import Path


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
