from types import SimpleNamespace

from deepagents.backends import FilesystemBackend

from graph.middlewares.versioned_patch import ReplacementHunk, VersionedPatchMiddleware, _digest
from graph.session_manager import SessionManager


def _runtime(call_id: str = "call-1", **context):
    return SimpleNamespace(tool_call_id=call_id, context=context)


def test_committed_external_artifact_supersedes_older_staged_lease(tmp_path) -> None:
    manager = SessionManager()
    state = tmp_path / "state"
    state.mkdir()
    manager.initialize(state)
    manager.create_session("lease-order-session")
    common = {
        "target_path": "/external/report.html",
        "goal_id": "goal-1",
        "goal_revision": 2,
        "run_id": "run-1",
        "query_id": "query-1",
    }
    manager.upsert_external_artifact_lease(
        "lease-order-session",
        {
            **common,
            "lease_id": "artifact-lease-old",
            "status": "staged",
            "created_at": 10.0,
            "expected_source_sha256": "sha256:old-source",
        },
    )
    manager.upsert_external_artifact_lease(
        "lease-order-session",
        {
            **common,
            "lease_id": "artifact-lease-committed",
            "status": "committed",
            "created_at": 20.0,
            "committed_at": 30.0,
            "expected_source_sha256": "sha256:old-source",
            "committed_sha256": "sha256:new-source",
        },
    )

    found = manager.find_staged_external_artifact_lease(
        "lease-order-session",
        run_id="run-2",
        query_id="query-2",
        target_path="/external/report.html",
        goal_id="goal-1",
        goal_revision=2,
    )

    assert found is None


def test_versioned_patch_rejects_stale_source_and_applies_atomic_replacements(tmp_path):
    path = tmp_path / "report.html"
    path.write_text("A\nB\n", encoding="utf-8")
    middleware = VersionedPatchMiddleware(
        FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    )
    patch_tool = next(tool for tool in middleware.tools if tool.name == "patch_file")

    conflict = patch_tool.func(
        file_path="/report.html",
        expected_sha256="sha256:stale",
        replacements=[ReplacementHunk(old_string="A", new_string="C")],
        runtime=_runtime(),
    )
    assert conflict.status == "error"
    assert "source version changed" in conflict.content
    assert path.read_text(encoding="utf-8") == "A\nB\n"

    current = path.read_text(encoding="utf-8")
    applied = patch_tool.func(
        file_path="/report.html",
        expected_sha256=_digest(current),
        replacements=[
            ReplacementHunk(old_string="A", new_string="C"),
            ReplacementHunk(old_string="B", new_string="D"),
        ],
        runtime=_runtime("call-2"),
    )
    assert applied.status == "success"
    assert path.read_text(encoding="utf-8") == "C\nD\n"


def test_external_artifact_lease_stages_validates_and_commits_exact_target(tmp_path):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external" / "report.html"
    workspace.mkdir()
    scratch.mkdir()
    state.mkdir()
    external.parent.mkdir()
    external.write_text("before", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("lease-session")
    for grant_type, capabilities in (
        ("external_file_read", ["read", "external_path"]),
        ("external_file_write", ["write", "external_path"]),
    ):
        session_manager.add_permission_grant(
            "lease-session",
            grant_type=grant_type,
            target_kind="exact_file",
            target=str(external.resolve()),
            capabilities=capabilities,
        )

    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
        },
        session_id="lease-session",
        workspace_root=workspace,
    )
    middleware = VersionedPatchMiddleware(backend)
    stage_tool = next(tool for tool in middleware.tools if tool.name == "stage_external_artifact")
    commit_tool = next(tool for tool in middleware.tools if tool.name == "commit_external_artifact")

    staged = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-stage",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert staged.status == "success"
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease("lease-session", lease_id)
    assert lease is not None
    assert lease["target_path"] == str(external.resolve())
    staged_host_path = scratch / str(lease["staged_path"]).removeprefix("/scratch/")
    assert staged_host_path.read_text(encoding="utf-8") == "before"
    staged_host_path.write_text("after", encoding="utf-8")

    reused = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-stage-retry",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert reused.status == "success"
    assert "reused" in reused.content
    assert f"lease_id={lease_id}" in reused.content
    assert staged_host_path.read_text(encoding="utf-8") == "after"

    external.write_text("concurrent", encoding="utf-8")
    replay_conflict = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-stage",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert replay_conflict.status == "error"
    assert "Stage conflict" in replay_conflict.content
    assert session_manager.get_external_artifact_lease(
        "lease-session", lease_id
    )["expected_source_sha256"] == lease["expected_source_sha256"]

    conflict = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime(
            "call-conflict",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert conflict.status == "error"
    assert "external target changed after staging" in conflict.content
    assert external.read_text(encoding="utf-8") == "concurrent"

    external.write_text("before", encoding="utf-8")
    committed = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime(
            "call-commit",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert committed.status == "success"
    assert external.read_text(encoding="utf-8") == "after"
    persisted = session_manager.get_external_artifact_lease("lease-session", lease_id)
    assert persisted is not None
    assert persisted["status"] == "committed"

    forged_replay = commit_tool.func(
        lease_id=lease_id,
        file_path="/workspace/not-the-authoritative-target.html",
        expected_source_sha256="sha256:forged",
        runtime=_runtime(
            "call-forged",
            session_id="lease-session",
            run_id="run-1",
            query_id="query-1",
        ),
    )
    assert forged_replay.status == "error"
    assert "exact target" in forged_replay.content


def test_external_artifact_lease_can_continue_in_another_run_of_same_goal(tmp_path):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external" / "report.html"
    state.mkdir()
    workspace.mkdir()
    scratch.mkdir()
    external.parent.mkdir()
    external.write_text("before", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("cross-run-lease-session")
    for grant_type, capabilities in (
        ("external_file_read", ["read", "external_path"]),
        ("external_file_write", ["write", "external_path"]),
    ):
        session_manager.add_permission_grant(
            "cross-run-lease-session",
            grant_type=grant_type,
            target_kind="exact_file",
            target=str(external.resolve()),
            capabilities=capabilities,
        )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    middleware = VersionedPatchMiddleware(
        PermissionedCompositeBackend(
            default=workspace_backend,
            routes={
                "/workspace/": workspace_backend,
                "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
            },
            session_id="cross-run-lease-session",
            workspace_root=workspace,
        )
    )
    stage_tool = next(tool for tool in middleware.tools if tool.name == "stage_external_artifact")
    commit_tool = next(tool for tool in middleware.tools if tool.name == "commit_external_artifact")
    staged = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime(
            "call-stage-cross-run",
            session_id="cross-run-lease-session",
            run_id="run-1",
            query_id="query-1",
            goal_id="goal-1",
            goal_revision=2,
        ),
    )
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease("cross-run-lease-session", lease_id)
    wrong_revision = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime(
            "call-commit-cross-run",
            session_id="cross-run-lease-session",
            run_id="run-2",
            query_id="query-2",
            goal_id="goal-1",
            goal_revision=3,
        ),
    )
    assert wrong_revision.status == "error"
    assert "different execution scope" in wrong_revision.content
    assert external.read_text(encoding="utf-8") == "before"

    continued = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime(
            "call-commit-next-run",
            session_id="cross-run-lease-session",
            run_id="run-2",
            query_id="query-2",
            goal_id="goal-1",
            goal_revision=2,
        ),
    )
    assert continued.status == "success"
    assert external.read_text(encoding="utf-8") == "before"


def test_expired_external_artifact_lease_can_be_renewed_without_losing_staged_edits(
    tmp_path,
):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external" / "report.html"
    state.mkdir()
    workspace.mkdir()
    scratch.mkdir()
    external.parent.mkdir()
    external.write_text("before", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("expired-lease-session")
    for grant_type, capabilities in (
        ("external_file_read", ["read", "external_path"]),
        ("external_file_write", ["write", "external_path"]),
    ):
        session_manager.add_permission_grant(
            "expired-lease-session",
            grant_type=grant_type,
            target_kind="exact_file",
            target=str(external.resolve()),
            capabilities=capabilities,
        )

    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    middleware = VersionedPatchMiddleware(
        PermissionedCompositeBackend(
            default=workspace_backend,
            routes={
                "/workspace/": workspace_backend,
                "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
            },
            session_id="expired-lease-session",
            workspace_root=workspace,
        )
    )
    stage_tool = next(
        tool for tool in middleware.tools if tool.name == "stage_external_artifact"
    )
    commit_tool = next(
        tool for tool in middleware.tools if tool.name == "commit_external_artifact"
    )
    context = {
        "session_id": "expired-lease-session",
        "run_id": "run-1",
        "query_id": "query-1",
    }
    staged = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime("call-stage", **context),
    )
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease(
        "expired-lease-session",
        lease_id,
    )
    assert lease is not None
    staged_host_path = scratch / str(lease["staged_path"]).removeprefix(
        "/scratch/"
    )
    staged_host_path.write_text("after", encoding="utf-8")
    lease["expires_at"] = 0
    session_manager.upsert_external_artifact_lease(
        "expired-lease-session",
        lease,
    )

    expired = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime("call-expired", **context),
    )
    assert expired.status == "error"
    assert "expired" in expired.content

    renewed = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime("call-restage", **context),
    )
    assert renewed.status == "success"
    assert "renewed after expiry" in renewed.content
    assert f"lease_id={lease_id}" in renewed.content
    assert staged_host_path.read_text(encoding="utf-8") == "after"

    committed = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime("call-commit", **context),
    )
    assert committed.status == "success"
    assert external.read_text(encoding="utf-8") == "after"


def test_missing_external_artifact_draft_is_rehydrated_from_current_source(tmp_path):
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external" / "report.html"
    for path in (state, workspace, scratch, external.parent):
        path.mkdir(exist_ok=True)
    external.write_text("source", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("missing-draft-session")
    session_manager.add_permission_grant(
        "missing-draft-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(external.resolve()),
        capabilities=["read", "external_path"],
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    middleware = VersionedPatchMiddleware(
        PermissionedCompositeBackend(
            default=workspace_backend,
            routes={
                "/workspace/": workspace_backend,
                "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
            },
            session_id="missing-draft-session",
            workspace_root=workspace,
        )
    )
    stage_tool = next(
        tool for tool in middleware.tools if tool.name == "stage_external_artifact"
    )
    context = {
        "session_id": "missing-draft-session",
        "run_id": "run-1",
        "query_id": "query-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
    }
    staged = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime("call-stage", **context),
    )
    lease_id = staged.content.split("lease_id=", 1)[1].split(";", 1)[0]
    lease = session_manager.get_external_artifact_lease(
        "missing-draft-session",
        lease_id,
    )
    assert lease is not None
    staged_host = scratch / str(lease["staged_path"]).removeprefix("/scratch/")
    staged_host.unlink()

    recovered = stage_tool.func(
        file_path=str(external.resolve()),
        runtime=_runtime("call-restage", **context),
    )

    assert recovered.status == "success"
    assert "rehydrated from the current source" in recovered.content
    assert staged_host.read_text(encoding="utf-8") == "source"
