from types import SimpleNamespace

from deepagents.backends import FilesystemBackend

from graph.middlewares.versioned_patch import ReplacementHunk, VersionedPatchMiddleware, _digest


def _runtime(call_id: str = "call-1", **context):
    return SimpleNamespace(tool_call_id=call_id, context=context)


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


def test_external_artifact_lease_cannot_be_committed_from_another_run(tmp_path):
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
    denied = commit_tool.func(
        lease_id=lease_id,
        file_path=str(external.resolve()),
        expected_source_sha256=lease["expected_source_sha256"],
        runtime=_runtime(
            "call-commit-cross-run",
            session_id="cross-run-lease-session",
            run_id="run-2",
            query_id="query-2",
            goal_id="goal-1",
            goal_revision=2,
        ),
    )
    assert denied.status == "error"
    assert "different Run/query" in denied.content
    assert external.read_text(encoding="utf-8") == "before"
