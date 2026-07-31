from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_permission_grant_cannot_recreate_a_deleted_session(tmp_path):
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)

    with pytest.raises(FileNotFoundError):
        session_manager.add_permission_grant(
            "missing-session",
            grant_type="external_file_read",
            target_kind="all_external_files",
            target="*",
            capabilities=["read", "external_path"],
        )

    assert not (tmp_path / "missing-session.json").exists()


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


def test_permission_grant_revision_changes_on_add_and_revoke(tmp_path):
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("permission-revision-session")
    target = tmp_path / "outside" / "note.txt"
    target.parent.mkdir()
    target.write_text("hello", encoding="utf-8")

    initial_grants, initial_revision = session_manager.permission_grants_snapshot("permission-revision-session")
    grant = session_manager.add_permission_grant(
        "permission-revision-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(target.resolve()),
        capabilities=["read", "external_path"],
    )
    added_grants, added_revision = session_manager.permission_grants_snapshot("permission-revision-session")
    session_manager.revoke_permission_grant("permission-revision-session", grant["id"])
    revoked_grants, revoked_revision = session_manager.permission_grants_snapshot("permission-revision-session")

    assert initial_grants == []
    assert initial_revision == 0
    assert [item["id"] for item in added_grants] == [grant["id"]]
    assert added_revision == 1
    assert revoked_grants == []
    assert revoked_revision == 2


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


@pytest.mark.parametrize(
    "target",
    [
        "/workspace/report.html",
        "/scratch/report.html",
        "/knowledge/report.md",
    ],
)
def test_external_grants_reject_internal_virtual_targets(tmp_path, target):
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("internal-grant-session")

    with pytest.raises(ValueError, match="cannot target internal virtual paths"):
        session_manager.add_permission_grant(
            "internal-grant-session",
            grant_type="external_file_read",
            target_kind="exact_file",
            target=target,
            capabilities=["read", "external_path"],
        )

    assert session_manager.list_permission_grants("internal-grant-session") == []


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


def test_external_read_grant_never_steals_virtual_scratch_routing(tmp_path):
    from deepagents.backends import FilesystemBackend

    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    state.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("virtual-scratch-routing-session")
    session_manager.add_permission_grant(
        "virtual-scratch-routing-session",
        grant_type="external_file_read",
        target_kind="all_external_files",
        target="*",
        capabilities=["read", "external_path"],
    )
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    target = scratch / "external-directories" / "directory-lease-1" / "chart.js"
    target.parent.mkdir(parents=True)
    target.write_text("const years = [2026];", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
        },
        session_id="virtual-scratch-routing-session",
        workspace_root=workspace,
    )

    result = backend.read(
        "/scratch/external-directories/directory-lease-1/chart.js",
        limit=20,
    )

    assert result.error is None
    assert result.file_data is not None
    assert result.file_data.get("content") == "const years = [2026];"


def test_permissioned_backend_never_mutates_managed_resources(tmp_path):
    from deepagents.backends import FilesystemBackend

    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("managed-readonly-session")
    workspace = tmp_path / "workspace"
    skills = workspace / "backend" / "skills"
    workspace.mkdir()
    skills.mkdir(parents=True)
    skill_file = skills / "SKILL.md"
    skill_file.write_text("original", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    skills_backend = FilesystemBackend(root_dir=skills, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend, "/skills/": skills_backend},
        session_id="managed-readonly-session",
        managed_readonly_roots=(skills,),
        workspace_root=workspace,
    )

    virtual_write = backend.write("/skills/new.md", "forged")
    virtual_edit = backend.edit("/skills/SKILL.md", "original", "forged")
    workspace_alias_edit = backend.edit(
        "/workspace/backend/skills/SKILL.md",
        "original",
        "forged",
    )
    relative_write = backend.write("backend/skills/relative.md", "forged")
    relative_edit = backend.edit(
        "backend/skills/SKILL.md",
        "original",
        "forged",
    )
    session_manager.add_permission_grant(
        "managed-readonly-session",
        grant_type="external_file_write",
        target_kind="exact_file",
        target=str(skill_file.resolve()),
        capabilities=["write", "external_path"],
    )
    absolute_edit = backend.edit(str(skill_file.resolve()), "original", "forged")

    assert virtual_write.error == "Managed resource is read-only: /skills/new.md"
    assert virtual_edit.error == "Managed resource is read-only: /skills/SKILL.md"
    assert workspace_alias_edit.error == ("Managed resource is read-only: /workspace/backend/skills/SKILL.md")
    assert relative_write.error == ("Managed resource is read-only: backend/skills/relative.md")
    assert relative_edit.error == ("Managed resource is read-only: backend/skills/SKILL.md")
    assert absolute_edit.error
    assert not (skills / "new.md").exists()
    assert not (skills / "relative.md").exists()
    assert skill_file.read_text(encoding="utf-8") == "original"


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
    assert (
        ExternalFilePermissionMiddleware._external_write_path(
            str(tmp_path / "outside.txt"),
            str(tmp_path / "workspace"),
        )
        == (tmp_path / "outside.txt").resolve()
    )
    assert (
        ExternalFilePermissionMiddleware._external_write_path(
            "/workspace/report.md",
            str(tmp_path / "workspace"),
        )
        is None
    )


def test_permission_middleware_treats_configured_knowledge_file_as_managed_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.session_manager import session_manager

    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    knowledge_file = knowledge_root / "imported" / "note.md"
    knowledge_file.parent.mkdir()
    knowledge_file.write_text("managed knowledge", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    session_manager.initialize(state_dir)
    session_manager.create_session("managed-knowledge-session")
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(knowledge_root))

    def unexpected_interrupt(_payload):
        raise AssertionError("configured knowledge reads must not request HITL permission")

    monkeypatch.setattr(permission_middleware_module, "interrupt", unexpected_interrupt)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": str(knowledge_file)},
                        "id": "call-managed-knowledge-read",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "managed-knowledge-session",
            "query_id": "query-managed-knowledge",
            "run_id": "run-managed-knowledge",
            "workspace_path": str(workspace),
        }
    )

    assert ExternalFilePermissionMiddleware().after_model(state, runtime) is None


def test_configured_knowledge_exception_is_read_only_and_symlink_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from graph.permission_middleware import ExternalFilePermissionMiddleware

    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    knowledge_file = knowledge_root / "note.md"
    knowledge_file.write_text("managed knowledge", encoding="utf-8")
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("outside", encoding="utf-8")
    link = knowledge_root / "outside-link.md"
    link.symlink_to(outside_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(knowledge_root))

    assert (
        ExternalFilePermissionMiddleware._external_read_path(
            str(knowledge_file),
            str(workspace),
        )
        is None
    )
    assert ExternalFilePermissionMiddleware._external_write_path(
        str(knowledge_file),
        str(workspace),
    ) == knowledge_file.resolve()
    assert ExternalFilePermissionMiddleware._external_read_path(
        str(link),
        str(workspace),
    ) == outside_file.resolve()


def test_external_delete_request_is_a_separate_capability(tmp_path):
    from graph.permission_resume import permission_resume_registry

    async def create_request():
        return permission_resume_registry.create_external_file_request(
            session_id="delete-preview-session",
            query_id="query-delete",
            tool_call_id="call-delete",
            path=tmp_path / "obsolete.txt",
            access="delete",
            operation="delete_file",
            change_preview={"expected_sha256": "sha256:current"},
        )

    request = asyncio.run(create_request())

    assert request["type"] == "external_file_delete"
    assert request["capabilities"] == ["delete", "external_path"]
    assert request["options"] == ["exact_file_session"]


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


@pytest.mark.parametrize(
    "root",
    [
        "/workspace",
        "/knowledge",
        "/semantic-assets",
        "/sql-guardrails",
        "/analytics-models",
        "/skills",
        "/large_tool_results",
        "/scratch",
    ],
)
def test_permission_middleware_accepts_virtual_namespace_roots(
    tmp_path,
    monkeypatch,
    root,
):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware

    def fail_interrupt(_payload):
        raise AssertionError("virtual namespace root must not request host permission")

    monkeypatch.setattr(permission_middleware_module, "interrupt", fail_interrupt)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "glob",
                        "args": {"path": root, "pattern": "**/*"},
                        "id": "call-search-virtual-root",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "virtual-root-session",
            "query_id": "query-virtual-root",
            "workspace_path": str(tmp_path),
        }
    )

    assert ExternalFilePermissionMiddleware().after_model(state, runtime) is None


def test_permission_middleware_accepts_versioned_patch_inside_virtual_scratch(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware

    def fail_interrupt(_payload):
        raise AssertionError("virtual scratch path must not request host-file permission")

    monkeypatch.setattr(permission_middleware_module, "interrupt", fail_interrupt)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "patch_file",
                        "args": {
                            "file_path": "/scratch/external/lease/report.html",
                            "expected_sha256": "sha256:abc",
                            "replacements": [{"old_string": "before", "new_string": "after"}],
                        },
                        "id": "call-patch-scratch",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "virtual-scratch-session",
            "query_id": "query-patch",
            "workspace_path": str(tmp_path),
        }
    )

    assert ExternalFilePermissionMiddleware().after_model(state, runtime) is None


def test_permission_middleware_never_requests_host_grant_for_scratch_copy(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware

    def fail_interrupt(_payload):
        raise AssertionError("scratch and workspace are internal authorities")

    monkeypatch.setattr(permission_middleware_module, "interrupt", fail_interrupt)
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "copy_file",
                        "args": {
                            "source_path": "/scratch/report.html",
                            "target_path": "/workspace/report.html",
                        },
                        "id": "call-copy-internal",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "internal-copy-session",
            "run_id": "run-internal-copy",
            "query_id": "query-internal-copy",
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
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": str(external), "offset": 100, "limit": 200},
                        "id": "call-read-external",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "middleware-read-route-session",
            "query_id": "query-read-route",
            "workspace_path": str(workspace),
        }
    )

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


def test_read_resource_rejects_http_url_as_a_local_path(tmp_path):
    from tools.read_resource_tool import ReadResourceTool

    url = "https://open.feishu.cn/document/guide.md"
    content = ReadResourceTool(
        session_id="resource-url-session",
        workspace_path=str(tmp_path),
    ).invoke({"resource": url})

    assert "use fetch_url" in content
    assert str(tmp_path) not in content


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


@pytest.mark.parametrize(
    "reason",
    [
        "network_access:lark-cli",
        "managed_skill_source_download:npx_skills_add",
    ],
)
def test_exact_shell_and_managed_skill_actions_reject_session_scope(tmp_path, reason):
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    session_id = f"exact-once-{reason.rsplit(':', 1)[-1]}"
    request_id = f"perm-req-{session_id}"
    session_manager.initialize(tmp_path)
    session_manager.create_session(session_id)
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    permission_resume_registry._pending[request_id] = future
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "tool_action",
        "session_id": session_id,
        "status": "pending",
        "fingerprint": f"sha256:{request_id}",
        "tool_name": "execute",
        "command": "lark-cli auth login" if "lark" in reason else "npx skills add https://example.com",
        "reason": reason,
        "risk": "network",
        "capabilities": ["execute", "network_access"],
    }

    response = TestClient(app).post(
        f"/api/sessions/{session_id}/permissions/tool-actions",
        json={"permission_request_id": request_id, "scope": "session"},
    )

    assert response.status_code == 400
    assert session_manager.list_permission_grants(session_id) == []
    assert not future.done()
    permission_resume_registry.resolve(request_id, {"type": "reject", "message": "test cleanup"})
    loop.close()


def test_session_network_grant_persists_shared_network_capability(tmp_path):
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("fetch-url-session")
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    request_id = "perm-req-session-network"
    permission_resume_registry._pending[request_id] = future
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "tool_action",
        "session_id": "fetch-url-session",
        "status": "pending",
        "fingerprint": "sha256:exact-url",
        "tool_name": "fetch_url",
        "command": '{"url": "https://example.com/report?month=1"}',
        "reason": "network_access:fetch_url",
        "risk": "network",
        "capabilities": ["execute", "network_access"],
        "session_target_kind": "capability",
        "session_target": "session_network_access",
        "session_scope_label": "本 Session 允许访问所有网络来源",
    }
    client = TestClient(app)

    response = client.post(
        "/api/sessions/fetch-url-session/permissions/tool-actions",
        json={"permission_request_id": request_id, "scope": "session"},
    )

    assert response.status_code == 200
    grant = response.json()["grant"]
    assert grant["target_kind"] == "capability"
    assert grant["target"] == "session_network_access"
    assert grant["scope"] == "session"
    assert grant["capabilities"] == ["execute", "network_access"]
    assert grant["metadata"]["session_target"] == "session_network_access"
    assert session_manager.consume_tool_action_permission(
        "fetch-url-session",
        "sha256:different-query",
        session_target_kind="capability",
        session_target="session_network_access",
        required_capabilities=["execute", "network_access"],
    )
    assert session_manager.consume_tool_action_permission(
        "fetch-url-session",
        "sha256:different-domain",
        session_target_kind="capability",
        session_target="session_network_access",
        required_capabilities=["execute", "network_access"],
    )
    assert not session_manager.consume_tool_action_permission(
        "fetch-url-session",
        "sha256:package-install",
        session_target_kind="capability",
        session_target="session_network_access",
        required_capabilities=["execute", "network_access", "package_install"],
    )
    loop.close()


def test_session_network_grant_auto_resumes_compatible_pending_requests(tmp_path):
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("parallel-network-session")
    loop = asyncio.new_event_loop()
    request_ids = [
        "perm-req-network-version",
        "perm-req-network-items",
        "perm-req-network-package",
    ]
    capabilities = [
        ["execute", "network_access"],
        ["execute", "network_access"],
        ["execute", "network_access", "package_install"],
    ]
    futures = []
    for index, request_id in enumerate(request_ids):
        future = loop.create_future()
        futures.append(future)
        permission_resume_registry._pending[request_id] = future
        permission_resume_registry._requests[request_id] = {
            "id": request_id,
            "type": "tool_action",
            "session_id": "parallel-network-session",
            "status": "pending",
            "fingerprint": f"sha256:parallel-{index}",
            "tool_name": "execute",
            "command": f"curl https://example.com/{index}",
            "reason": "network_access:execute",
            "risk": "network",
            "capabilities": capabilities[index],
            "session_target_kind": "capability",
            "session_target": "session_network_access",
        }

    response = TestClient(app).post(
        "/api/sessions/parallel-network-session/permissions/tool-actions",
        json={"permission_request_id": request_ids[0], "scope": "session"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_resumed_permission_request_ids"] == [request_ids[1]]
    assert futures[0].result()["type"] == "approve"
    assert futures[1].result()["grant_id"] == payload["grant"]["id"]
    assert not futures[2].done()
    assert permission_resume_registry.get(request_ids[1])["status"] == "resolved"
    assert permission_resume_registry.get(request_ids[2])["status"] == "pending"
    permission_resume_registry.resolve(request_ids[2], {"type": "reject", "message": "test cleanup"})
    loop.close()


def test_fetch_url_once_grant_stays_exact_fingerprint(tmp_path):
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("fetch-url-once-session")
    loop = asyncio.new_event_loop()
    request_id = "perm-req-fetch-url-once"
    permission_resume_registry._pending[request_id] = loop.create_future()
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "tool_action",
        "session_id": "fetch-url-once-session",
        "status": "pending",
        "fingerprint": "sha256:exact-url",
        "tool_name": "fetch_url",
        "command": '{"url": "https://example.com/report?month=1"}',
        "reason": "network_access:fetch_url",
        "risk": "network",
        "session_target_kind": "network_origin",
        "session_target": "https://example.com",
        "session_scope_label": "本 Session 允许访问 example.com",
    }
    client = TestClient(app)

    response = client.post(
        "/api/sessions/fetch-url-once-session/permissions/tool-actions",
        json={"permission_request_id": request_id, "scope": "once"},
    )

    assert response.status_code == 200
    grant = response.json()["grant"]
    assert grant["target_kind"] == "fingerprint"
    assert grant["target"] == "sha256:exact-url"
    assert grant["scope"] == "once"
    assert "session_target" not in grant["metadata"]
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


def test_permission_api_grants_exact_external_delete_separately(tmp_path):
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    session_manager.initialize(tmp_path)
    session_manager.create_session("delete-api-session")
    target = (tmp_path / "outside" / "obsolete.txt").resolve()
    target.parent.mkdir()
    target.write_text("obsolete", encoding="utf-8")
    request_id = "perm-req-delete-test"
    loop = asyncio.new_event_loop()
    permission_resume_registry._pending[request_id] = loop.create_future()
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "external_file_delete",
        "session_id": "delete-api-session",
        "path": str(target),
        "status": "pending",
    }

    response = TestClient(app).post(
        "/api/sessions/delete-api-session/permissions/external-files",
        json={
            "target_kind": "exact_file",
            "path": str(target),
            "permission_request_id": request_id,
        },
    )

    assert response.status_code == 200
    grant = response.json()["grant"]
    assert grant["type"] == "external_file_delete"
    assert grant["capabilities"] == ["delete", "external_path"]
    assert session_manager.has_external_file_delete_permission(
        "delete-api-session",
        target,
    )
    assert not session_manager.has_external_file_write_permission(
        "delete-api-session",
        target,
    )
    loop.close()
