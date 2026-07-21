from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

from deepagents.backends import FilesystemBackend


def _runtime(
    call_id: str,
    *,
    run_id: str = "run-1",
    query_id: str = "query-1",
):
    return SimpleNamespace(
        tool_call_id=call_id,
        context={
            "session_id": "directory-session",
            "run_id": run_id,
            "query_id": query_id,
            "goal_id": "goal-1",
            "goal_revision": 1,
        },
    )


def _setup(tmp_path: Path):
    from deepagents.backends import CompositeBackend

    from graph.middlewares.external_directory import ExternalDirectoryMiddleware
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external-project"
    for path in (state, workspace, scratch, external):
        path.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("directory-session")
    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=workspace, virtual_mode=True),
        routes={
            "/workspace/": FilesystemBackend(root_dir=workspace, virtual_mode=True),
            "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
        },
    )
    middleware = ExternalDirectoryMiddleware(backend)
    tools = {tool.name: tool for tool in middleware.tools}
    return external, scratch, tools, session_manager


def _grant(session_manager, path: Path, *, access: str, run_id: str = "run-1") -> None:
    session_manager.add_permission_grant(
        "directory-session",
        grant_type=f"external_directory_{access}",
        target_kind="exact_directory",
        target=str(path.resolve()),
        capabilities=[access, "recursive", "external_path"],
        scope="run",
        source="user",
        metadata={"run_id": run_id},
    )


def test_external_directory_snapshot_prepare_and_commit(tmp_path: Path) -> None:
    external, scratch, tools, session_manager = _setup(tmp_path)
    (external / "src").mkdir()
    (external / "src" / "keep.txt").write_text("before", encoding="utf-8")
    (external / "delete.txt").write_text("delete", encoding="utf-8")
    (external / ".env").write_text("SECRET=1", encoding="utf-8")
    (external / "node_modules").mkdir()
    (external / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (external / "escape-link").symlink_to(outside)
    _grant(session_manager, external, access="read")

    staged = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-stage"),
    )

    assert staged.status == "success"
    lease = staged.artifact["external_directory_lease"]
    staged_host = scratch / str(lease["staged_dir"]).removeprefix("/scratch/")
    assert (staged_host / "src" / "keep.txt").read_text(encoding="utf-8") == "before"
    assert not (staged_host / ".env").exists()
    assert not (staged_host / "node_modules").exists()
    assert not (staged_host / "escape-link").exists()

    (staged_host / "src" / "keep.txt").write_text("after", encoding="utf-8")
    (staged_host / "delete.txt").unlink()
    (staged_host / "new.txt").write_text("new", encoding="utf-8")
    prepared = tools["prepare_external_directory_commit"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        runtime=_runtime("call-prepare"),
    )

    assert prepared.status == "success"
    plan = prepared.artifact["external_directory_commit_plan"]
    assert plan["added"] == ["new.txt"]
    assert plan["modified"] == ["src/keep.txt"]
    assert plan["deleted"] == ["delete.txt"]

    _grant(session_manager, external, access="write")
    committed = tools["commit_external_directory"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        plan_digest=plan["plan_digest"],
        runtime=_runtime("call-commit"),
    )

    assert committed.status == "success"
    assert (external / "src" / "keep.txt").read_text(encoding="utf-8") == "after"
    assert (external / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (external / "delete.txt").exists()
    assert (external / ".env").read_text(encoding="utf-8") == "SECRET=1"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_external_directory_commit_detects_source_and_staged_conflicts(tmp_path: Path) -> None:
    external, scratch, tools, session_manager = _setup(tmp_path)
    (external / "report.txt").write_text("v1", encoding="utf-8")
    _grant(session_manager, external, access="read")
    _grant(session_manager, external, access="write")
    staged = tools["stage_external_directory"].func(directory_path=str(external), runtime=_runtime("call-stage"))
    lease = staged.artifact["external_directory_lease"]
    staged_host = scratch / str(lease["staged_dir"]).removeprefix("/scratch/")
    (staged_host / "report.txt").write_text("v2", encoding="utf-8")
    prepared = tools["prepare_external_directory_commit"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        runtime=_runtime("call-prepare"),
    )
    plan = prepared.artifact["external_directory_commit_plan"]

    (external / "report.txt").write_text("concurrent", encoding="utf-8")
    source_conflict = tools["commit_external_directory"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        plan_digest=plan["plan_digest"],
        runtime=_runtime("call-commit"),
    )
    assert source_conflict.status == "error"
    assert "source directory changed" in source_conflict.content

    (external / "report.txt").write_text("v1", encoding="utf-8")
    (staged_host / "report.txt").write_text("v3-after-review", encoding="utf-8")
    staged_conflict = tools["commit_external_directory"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        plan_digest=plan["plan_digest"],
        runtime=_runtime("call-commit-2"),
    )
    assert staged_conflict.status == "error"
    assert "staged directory changed after review" in staged_conflict.content
    assert (external / "report.txt").read_text(encoding="utf-8") == "v1"


def test_external_directory_goal_draft_rebinds_across_runs(tmp_path: Path) -> None:
    external, scratch, tools, session_manager = _setup(tmp_path)
    (external / "report.txt").write_text("before", encoding="utf-8")
    _grant(session_manager, external, access="read", run_id="run-1")
    staged = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-stage"),
    )
    lease = staged.artifact["external_directory_lease"]
    staged_host = scratch / str(lease["staged_dir"]).removeprefix("/scratch/")
    (staged_host / "report.txt").write_text("after", encoding="utf-8")

    _grant(session_manager, external, access="read", run_id="run-2")
    rebound = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-restage", run_id="run-2", query_id="query-2"),
    )

    assert rebound.status == "success"
    assert "rebound to this Run" in rebound.content
    assert rebound.artifact["external_directory_lease"]["lease_id"] == lease["lease_id"]
    assert (staged_host / "report.txt").read_text(encoding="utf-8") == "after"

    prepared = tools["prepare_external_directory_commit"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        runtime=_runtime("call-prepare", run_id="run-2", query_id="query-2"),
    )
    assert prepared.status == "success"
    _grant(session_manager, external, access="write", run_id="run-2")
    committed = tools["commit_external_directory"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        plan_digest=prepared.artifact["external_directory_commit_plan"]["plan_digest"],
        runtime=_runtime("call-commit", run_id="run-2", query_id="query-2"),
    )
    assert committed.status == "success"
    assert (external / "report.txt").read_text(encoding="utf-8") == "after"


def test_missing_external_directory_draft_is_rehydrated_from_current_source(
    tmp_path: Path,
) -> None:
    external, scratch, tools, session_manager = _setup(tmp_path)
    (external / "report.txt").write_text("source", encoding="utf-8")
    _grant(session_manager, external, access="read")
    staged = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-stage"),
    )
    lease = staged.artifact["external_directory_lease"]
    staged_host = scratch / str(lease["staged_dir"]).removeprefix("/scratch/")
    shutil.rmtree(staged_host)

    recovered = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-restage"),
    )

    assert recovered.status == "success"
    assert "rehydrated from the current source" in recovered.content
    assert (staged_host / "report.txt").read_text(encoding="utf-8") == "source"


def test_unchanged_directory_draft_rebases_after_source_changes(tmp_path: Path) -> None:
    external, scratch, tools, session_manager = _setup(tmp_path)
    (external / "report.txt").write_text("before", encoding="utf-8")
    _grant(session_manager, external, access="read")
    staged = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-stage"),
    )
    old_lease = staged.artifact["external_directory_lease"]

    (external / "report.txt").write_text("committed-by-exact-file-lease", encoding="utf-8")
    refreshed = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-restage"),
    )

    assert refreshed.status == "success"
    new_lease = refreshed.artifact["external_directory_lease"]
    assert new_lease["lease_id"] != old_lease["lease_id"]
    assert session_manager.get_external_directory_lease(
        "directory-session", old_lease["lease_id"]
    )["status"] == "superseded"
    refreshed_host = scratch / str(new_lease["staged_dir"]).removeprefix("/scratch/")
    assert (refreshed_host / "report.txt").read_text(encoding="utf-8") == "committed-by-exact-file-lease"


def test_changed_directory_draft_is_not_silently_rebased(tmp_path: Path) -> None:
    external, scratch, tools, session_manager = _setup(tmp_path)
    (external / "report.txt").write_text("before", encoding="utf-8")
    _grant(session_manager, external, access="read")
    staged = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-stage"),
    )
    lease = staged.artifact["external_directory_lease"]
    staged_host = scratch / str(lease["staged_dir"]).removeprefix("/scratch/")
    (staged_host / "report.txt").write_text("draft-edit", encoding="utf-8")
    (external / "report.txt").write_text("concurrent-edit", encoding="utf-8")

    conflict = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-restage"),
    )

    assert conflict.status == "error"
    assert "Goal draft also has edits" in conflict.content
    assert session_manager.get_external_directory_lease(
        "directory-session", lease["lease_id"]
    )["status"] == "staged"


def test_external_directory_permission_is_exact_and_run_scoped(tmp_path: Path) -> None:
    external, _scratch, _tools, session_manager = _setup(tmp_path)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    _grant(session_manager, external, access="read", run_id="run-1")

    assert session_manager.has_external_directory_permission(
        "directory-session", external, access="read", run_id="run-1"
    )
    assert not session_manager.has_external_directory_permission(
        "directory-session", external, access="read", run_id="run-2"
    )
    assert not session_manager.has_external_directory_permission(
        "directory-session", sibling, access="read", run_id="run-1"
    )


def test_user_supplied_external_directory_gets_lease_instructions(tmp_path: Path) -> None:
    from graph.deepagents_manager import DeepAgentsAgentManager

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()

    content = DeepAgentsAgentManager._build_user_content(
        f"结合 {external} 里的文件更新报告",
        session_id="directory-session",
        workspace_path=workspace,
    )

    assert isinstance(content, str)
    assert "stage_external_directory(directory_path=原始绝对目录)" in content
    assert "/scratch/external-directories/<lease_id>/" in content
    assert "prepare_external_directory_commit" in content
    assert "commit_external_directory" in content


def test_precise_external_file_does_not_infer_parent_directory(tmp_path: Path) -> None:
    from graph.deepagents_manager import DeepAgentsAgentManager

    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    target = external / "产品配置分析_2026.html"
    workspace.mkdir()
    external.mkdir()
    target.write_text("<html></html>", encoding="utf-8")

    content = DeepAgentsAgentManager._build_user_content(
        f"刷新{target}，同步更新图表趋势分析",
        session_id="file-session",
        workspace_path=workspace,
    )

    assert isinstance(content, str)
    assert "stage_external_artifact(file_path=原始绝对路径)" in content
    assert "stage_external_directory(directory_path=该文件的父目录)" in content
    assert "如果读取后确认必须发现同目录依赖文件" in content


def test_stage_external_directory_receives_runtime_through_tool_node(tmp_path: Path) -> None:
    from deepagents.backends import FilesystemBackend
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    from graph.middlewares.external_directory import ExternalDirectoryMiddleware

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    tool = ExternalDirectoryMiddleware(
        FilesystemBackend(root_dir=scratch, virtual_mode=True)
    ).tools[0]
    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)

    result = builder.compile().invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "stage_external_directory",
                            "args": {"directory_path": str(tmp_path)},
                            "id": "call-directory-runtime",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        context={"session_id": "", "run_id": "", "query_id": ""},
    )

    tool_message = result["messages"][-1]
    assert tool_message.status == "error"
    assert "requires an active Session, Run, and query" in tool_message.content


def test_permission_middleware_requests_run_scoped_directory_write(tmp_path: Path, monkeypatch) -> None:
    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    session_manager.initialize(state_dir)
    session_manager.create_session("directory-session")
    external = tmp_path / "external"
    external.mkdir()
    session_manager.upsert_external_directory_lease(
        "directory-session",
        {
            "lease_id": "lease-preview",
            "commit_plan": {
                "added": ["new.txt"],
                "modified": ["report.html"],
                "deleted": ["old.txt"],
            },
        },
    )
    captured: dict = {}

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
                        "name": "commit_external_directory",
                        "args": {
                            "directory_path": str(external),
                            "lease_id": "lease-preview",
                            "plan_digest": "sha256:plan",
                        },
                        "id": "call-directory-commit",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "directory-session",
            "query_id": "query-1",
            "run_id": "run-1",
            "workspace_path": str(tmp_path / "workspace"),
        }
    )

    async def invoke():
        return ExternalFilePermissionMiddleware().after_model(state, runtime)

    assert asyncio.run(invoke()) is None
    request = captured["request"]
    assert request["type"] == "external_directory_write"
    assert request["target_kind"] == "exact_directory"
    assert request["run_id"] == "run-1"
    assert request["change_preview"]["新增文件"] == "new.txt"
    permission_resume_registry._requests.pop(request["id"], None)
    permission_resume_registry._pending.pop(request["id"], None)


def test_permission_api_grants_exact_directory_for_current_run(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    external = tmp_path / "external"
    state.mkdir()
    external.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("directory-api-session")
    request_id = "perm-req-directory-api"
    loop = asyncio.new_event_loop()
    permission_resume_registry._pending[request_id] = loop.create_future()
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "external_directory_write",
        "session_id": "directory-api-session",
        "run_id": "run-7",
        "path": str(external.resolve()),
        "status": "pending",
        "change_preview": {"修改文件": "report.html"},
    }

    response = TestClient(app).post(
        "/api/sessions/directory-api-session/permissions/external-files",
        json={
            # Compatibility with a stale frontend that still renders the
            # directory request as an exact-file permission card.
            "target_kind": "exact_file",
            "path": str(external),
            "permission_request_id": request_id,
        },
    )

    assert response.status_code == 200
    grant = response.json()["grant"]
    assert grant["type"] == "external_directory_write"
    assert grant["scope"] == "run"
    assert grant["target_kind"] == "exact_directory"
    assert grant["metadata"]["run_id"] == "run-7"
    assert grant["metadata"]["requested_target_kind"] == "exact_file"
    assert session_manager.has_external_directory_permission(
        "directory-api-session",
        external,
        access="write",
        run_id="run-7",
    )

    broad_request_id = "perm-req-directory-api-broad-client"
    permission_resume_registry._pending[broad_request_id] = loop.create_future()
    permission_resume_registry._requests[broad_request_id] = {
        "id": broad_request_id,
        "type": "external_directory_read",
        "session_id": "directory-api-session",
        "run_id": "run-8",
        "path": str(external.resolve()),
        "status": "pending",
    }
    broad_response = TestClient(app).post(
        "/api/sessions/directory-api-session/permissions/external-files",
        json={
            "target_kind": "all_external_files",
            "permission_request_id": broad_request_id,
        },
    )
    assert broad_response.status_code == 200
    broad_grant = broad_response.json()["grant"]
    assert broad_grant["target_kind"] == "exact_directory"
    assert broad_grant["target"] == str(external.resolve())
    assert broad_grant["scope"] == "run"
    assert broad_grant["metadata"]["requested_target_kind"] == "all_external_files"
    loop.close()
