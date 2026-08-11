from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import FilesystemBackend
from langchain_core.messages import ToolMessage


def _runtime(
    call_id: str,
    *,
    run_id: str = "run-1",
    query_id: str = "query-1",
    goal_id: str | None = "goal-1",
):
    context = {
        "session_id": "directory-session",
        "run_id": run_id,
        "query_id": query_id,
    }
    if goal_id:
        context.update({"goal_id": goal_id, "goal_revision": 1})
    return SimpleNamespace(
        tool_call_id=call_id,
        context=context,
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


def _grant(
    session_manager,
    path: Path,
    *,
    access: str,
    run_id: str = "run-1",
    scope: str = "run",
    bindings: dict | None = None,
) -> dict:
    return session_manager.add_permission_grant(
        "directory-session",
        grant_type=f"external_directory_{access}",
        target_kind="exact_directory",
        target=str(path.resolve()),
        capabilities=[
            access,
            *(["delete"] if access == "write" else []),
            "recursive",
            "external_path",
        ],
        scope=scope,
        source="user",
        metadata={"run_id": run_id},
        bindings=bindings,
    )


def _start_bound_run(
    session_manager,
    *,
    query_id: str,
    backend_id: str,
    workspace_id: str = "workspace:stable",
):
    from harness.coordinators import HarnessRunCoordinator
    from harness.models import RunStatus

    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="directory-session",
        query_id=query_id,
        objective="inspect external directory",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.bind_execution_snapshot(
        run,
        {
            "backend_mode": "docker",
            "backend_id": backend_id,
            "workspace_id": workspace_id,
        },
    )
    coordinator.transition(run, RunStatus.RUNNING)
    return coordinator, run


def _active_shell_bindings(session_manager, run_id: str) -> dict:
    from graph.permission_policy import RunPermissionContext

    state = session_manager.get_run_state("directory-session", run_id)
    return RunPermissionContext.from_config_snapshot(
        state["config_snapshot"]
    ).shell_grant_bindings()


def test_shell_directory_grants_are_atomic_and_idempotent(tmp_path: Path) -> None:
    from graph.permission_policy import ShellDirectoryGrantSpec

    external, _scratch, _tools, session_manager = _setup(tmp_path)
    _coordinator, run = _start_bound_run(
        session_manager,
        query_id="query-shell-atomic",
        backend_id="container:atomic",
    )
    bindings = _active_shell_bindings(session_manager, run.run_id)
    specs = [
        ShellDirectoryGrantSpec(target=str(external.resolve()), access="read"),
        ShellDirectoryGrantSpec(target=str(external.resolve()), access="write"),
    ]

    before_grants, before_revision = session_manager.permission_grants_snapshot(
        "directory-session"
    )
    first = session_manager.add_shell_directory_grants_atomic(
        "directory-session",
        grant_specs=specs,
        scope="run",
        run_id=run.run_id,
        bindings=bindings,
    )
    after_grants, after_revision = session_manager.permission_grants_snapshot(
        "directory-session"
    )
    repeated = session_manager.add_shell_directory_grants_atomic(
        "directory-session",
        grant_specs=specs,
        scope="run",
        run_id=run.run_id,
        bindings=bindings,
    )
    final_grants, final_revision = session_manager.permission_grants_snapshot(
        "directory-session"
    )

    assert before_grants == []
    assert len(first) == 2
    assert {grant["binding_schema_version"] for grant in first} == {3}
    assert all("shell_access" in grant["capabilities"] for grant in first)
    assert len(after_grants) == 2
    assert after_revision == before_revision + 1
    assert [grant["id"] for grant in repeated] == [grant["id"] for grant in first]
    assert final_grants == after_grants
    assert final_revision == after_revision


@pytest.mark.parametrize(
    "failure_mode",
    ["write_without_read", "invalid_late_entry"],
)
def test_shell_directory_grant_batch_failure_leaves_no_authority(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    from graph.permission_policy import ShellDirectoryGrantSpec

    external, _scratch, _tools, session_manager = _setup(tmp_path)
    _coordinator, run = _start_bound_run(
        session_manager,
        query_id="query-shell-atomic-failure",
        backend_id="container:atomic-failure",
    )
    bindings = _active_shell_bindings(session_manager, run.run_id)
    target = str(external.resolve())
    specs = (
        [ShellDirectoryGrantSpec(target=target, access="write")]
        if failure_mode == "write_without_read"
        else [
            ShellDirectoryGrantSpec(target=target, access="read"),
            ShellDirectoryGrantSpec(target=target, access="write"),
            ShellDirectoryGrantSpec(target=target, access="execute"),
        ]
    )
    _grants, before_revision = session_manager.permission_grants_snapshot(
        "directory-session"
    )

    with pytest.raises(ValueError):
        session_manager.add_shell_directory_grants_atomic(
            "directory-session",
            grant_specs=specs,
            scope="run",
            run_id=run.run_id,
            bindings=bindings,
        )
    grants, revision = session_manager.permission_grants_snapshot(
        "directory-session"
    )
    assert grants == []
    assert revision == before_revision


def test_permission_migration_repairs_downgraded_native_shell_grants(
    tmp_path: Path,
) -> None:
    from graph.permission_policy import ShellDirectoryGrantSpec

    external, _scratch, _tools, session_manager = _setup(tmp_path)
    _coordinator, run = _start_bound_run(
        session_manager,
        query_id="query-shell-repair",
        backend_id="kernel:shell-repair",
    )
    bindings = _active_shell_bindings(session_manager, run.run_id)
    grants = session_manager.add_shell_directory_grants_atomic(
        "directory-session",
        grant_specs=[ShellDirectoryGrantSpec(target=str(external), access="read")],
        scope="run",
        run_id=run.run_id,
        bindings=bindings,
    )
    data = session_manager._read_file("directory-session")
    stored = next(
        item for item in data["permissions"]["grants"] if item["id"] == grants[0]["id"]
    )
    stored["binding_schema_version"] = 2
    stored["semantic_key"] = "sha256:stale-v2-key"
    session_manager._write_file("directory-session", data)

    session_manager.migrate_permission_grants("directory-session")

    active = session_manager.list_permission_grants("directory-session")
    assert len(active) == 1
    assert active[0]["binding_schema_version"] == 3
    assert active[0]["semantic_key"] != "sha256:stale-v2-key"


def test_shell_directory_grants_reject_incomplete_run_bindings(tmp_path: Path) -> None:
    from graph.permission_policy import ShellDirectoryGrantSpec

    external, _scratch, _tools, session_manager = _setup(tmp_path)
    _coordinator, run = _start_bound_run(
        session_manager,
        query_id="query-shell-binding-failure",
        backend_id="container:binding-failure",
    )
    bindings = _active_shell_bindings(session_manager, run.run_id)
    bindings.pop("isolation_policy_id")

    with pytest.raises(ValueError, match="bindings"):
        session_manager.add_shell_directory_grants_atomic(
            "directory-session",
            grant_specs=[
                ShellDirectoryGrantSpec(
                    target=str(external.resolve()),
                    access="read",
                )
            ],
            scope="run",
            run_id=run.run_id,
            bindings=bindings,
        )

    grants, revision = session_manager.permission_grants_snapshot(
        "directory-session"
    )
    assert grants == []
    assert revision == 0


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
    deleted_delivery = session_manager.register_delivered_artifact(
        "directory-session",
        target_path=str((external / "delete.txt").resolve()),
        content_sha256="sha256:" + hashlib.sha256(b"delete").hexdigest(),
        source_run_id="run-before-delete",
        source_query_id="query-before-delete",
    )

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
    refused = tools["prepare_external_directory_commit"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        runtime=_runtime("call-prepare-undeclared"),
    )
    assert refused.status == "error"
    assert "undeclared new files" in refused.content
    assert "/scratch/validation/" in refused.content
    prepared = tools["prepare_external_directory_commit"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        declared_delivery_files=["new.txt"],
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
    delivered = session_manager.list_delivered_artifacts(
        "directory-session", include_inactive=False
    )
    assert {Path(item["target_path"]).name for item in delivered} == {
        "keep.txt",
        "new.txt",
    }
    assert all(item["role"] == "delivered" for item in delivered)
    tombstone = next(
        item
        for item in session_manager.list_delivered_artifacts("directory-session")
        if item["artifact_id"] == deleted_delivery["artifact_id"]
    )
    assert tombstone["status"] == "deleted"
    assert tombstone["stale_reason"] == "deleted_by_committed_directory_plan"
    from harness.verification_activations import build_verification_activations

    activations = build_verification_activations(
        run_id="run-directory",
        query_id="query-directory",
        tool_call_id="call-commit",
        tool_name="commit_external_directory",
        args={
            "lease_id": lease["lease_id"],
            "directory_path": str(external),
            "plan_digest": plan["plan_digest"],
        },
        result=committed,
        session_id="directory-session",
    )
    artifact_activation = next(item for item in activations if item.pack == "artifact")
    assert len(
        [
            ref
            for ref in artifact_activation.evidence_refs
            if ref.get("kind") == "artifact_write"
        ]
    ) == 2


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


def test_external_directory_code_commit_requires_receipt_for_each_exact_draft(
    tmp_path: Path,
) -> None:
    from harness.models import RunRecord, RunStatus, ValidationReceipt, VerificationActivation

    external, scratch, tools, session_manager = _setup(tmp_path)
    (external / "app.js").write_text("const value = 1;\n", encoding="utf-8")
    run = RunRecord(
        run_id="run-1",
        query_id="query-1",
        session_id="directory-session",
        objective="update app.js",
        status=RunStatus.PREPARING,
    )
    session_manager.start_harness_run("directory-session", run.model_dump(mode="json"))
    session_manager.transition_run_status(
        "directory-session", run.run_id, RunStatus.RUNNING.value
    )
    _grant(session_manager, external, access="read")
    _grant(session_manager, external, access="write")

    runtime = _runtime("call-stage", goal_id=None)
    staged = tools["stage_external_directory"].func(
        directory_path=str(external), runtime=runtime
    )
    lease = staged.artifact["external_directory_lease"]
    staged_host = scratch / str(lease["staged_dir"]).removeprefix("/scratch/")
    draft = "const value = 2;\n"
    (staged_host / "app.js").write_text(draft, encoding="utf-8")
    draft_sha = "sha256:" + hashlib.sha256(draft.encode()).hexdigest()
    prepared = tools["prepare_external_directory_commit"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        runtime=_runtime("call-prepare", goal_id=None),
    )
    plan = prepared.artifact["external_directory_commit_plan"]

    missing = tools["commit_external_directory"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        plan_digest=plan["plan_digest"],
        validation_receipt_ids=[],
        runtime=_runtime("call-missing", goal_id=None),
    )
    assert missing.status == "error"
    assert "validation gate" in missing.content
    assert (external / "app.js").read_text(encoding="utf-8") == "const value = 1;\n"

    def persist(receipt: ValidationReceipt, call_id: str) -> None:
        activation = VerificationActivation(
            activation_id=f"activation-{call_id}",
            run_id="run-1",
            query_id="query-1",
            tool_call_id=call_id,
            tool_name="execute",
            pack="code",
            evidence_refs=[
                {
                    "kind": "validation_receipt",
                    **receipt.model_dump(mode="json"),
                    "material": True,
                }
            ],
        )
        session_manager.append_run_verification_activation(
            "directory-session", "run-1", activation.model_dump(mode="json")
        )

    failed = ValidationReceipt(
        validation_receipt_id="directory-validation-failed",
        run_id="run-1",
        validator_kind="javascript_syntax",
        artifact_refs=[
            {
                "artifact_id": "artifact-app-js",
                "content_sha256": draft_sha,
                "path": str((external / "app.js").resolve()),
            }
        ],
        command_evidence_ref="sha256:failed",
        exit_code=1,
        checks_failed=1,
        status="failed",
        failure_class="invocation_failure",
        commit_authority=True,
        obligation_key="javascript_syntax:node-check/v1",
        created_at=1.0,
    )
    persist(failed, "call-failed")
    blocked = tools["commit_external_directory"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        plan_digest=plan["plan_digest"],
        validation_receipt_ids=[failed.validation_receipt_id],
        runtime=_runtime("call-blocked", goal_id=None),
    )
    assert blocked.status == "error"
    assert "blocking_failed_receipts" in blocked.content

    passed = ValidationReceipt(
        validation_receipt_id="directory-validation-passed",
        run_id="run-1",
        validator_kind="javascript_syntax",
        artifact_refs=failed.artifact_refs,
        command_evidence_ref="sha256:passed",
        exit_code=0,
        status="passed",
        commit_authority=True,
        obligation_key="javascript_syntax:node-check/v1",
        created_at=2.0,
    )
    persist(passed, "call-passed")
    committed = tools["commit_external_directory"].func(
        lease_id=lease["lease_id"],
        directory_path=str(external),
        plan_digest=plan["plan_digest"],
        validation_receipt_ids=[passed.validation_receipt_id],
        runtime=_runtime("call-commit", goal_id=None),
    )
    assert committed.status == "success"
    assert (external / "app.js").read_text(encoding="utf-8") == draft


def test_directory_draft_validation_receipt_uses_formal_target_identity(
    tmp_path: Path,
) -> None:
    from harness.models import RunRecord, RunStatus
    from harness.verification_activations import build_verification_activations

    external, scratch, tools, session_manager = _setup(tmp_path)
    (external / "app.js").write_text("const value = 1;\n", encoding="utf-8")
    run = RunRecord(
        run_id="run-1",
        query_id="query-1",
        session_id="directory-session",
        objective="validate directory draft",
        status=RunStatus.PREPARING,
        config_snapshot={
            "execution": {
                "scratch_host_path": str(scratch.resolve()),
                "workspace_id": "workspace:test",
            }
        },
    )
    session_manager.start_harness_run("directory-session", run.model_dump(mode="json"))
    session_manager.transition_run_status(
        "directory-session", run.run_id, RunStatus.RUNNING.value
    )
    _grant(session_manager, external, access="read")
    staged = tools["stage_external_directory"].func(
        directory_path=str(external),
        runtime=_runtime("call-stage", goal_id=None),
    )
    lease = staged.artifact["external_directory_lease"]
    staged_path = f"{lease['staged_dir']}/app.js"
    activations = build_verification_activations(
        run_id="run-1",
        query_id="query-1",
        tool_call_id="call-node-check",
        tool_name="execute",
        args={"command": f"node --check {staged_path}"},
        result=ToolMessage(
            content="Exit code: 0",
            name="execute",
            tool_call_id="call-node-check",
            status="success",
        ),
        session_id="directory-session",
        workspace_path=str((tmp_path / "workspace").resolve()),
    )
    receipt = next(
        ref
        for activation in activations
        for ref in activation.evidence_refs
        if ref.get("kind") == "validation_receipt"
    )
    assert receipt["artifact_refs"] == [
        {
            "artifact_id": receipt["artifact_refs"][0]["artifact_id"],
            "content_sha256": "sha256:"
            + hashlib.sha256(b"const value = 1;\n").hexdigest(),
            "path": str((external / "app.js").resolve()),
            "observed_path": staged_path,
        }
    ]


def test_exact_file_and_directory_staging_share_one_authoritative_draft_claim(
    tmp_path: Path,
) -> None:
    from graph.middlewares.external_directory import ExternalDirectoryMiddleware
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from graph.session_manager import session_manager
    from tools.filesystem.factory import VersionedPatchMiddleware

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    external = tmp_path / "external"
    for path in (state, workspace, scratch, external):
        path.mkdir()
    target = external / "app.js"
    target.write_text("const value = 1;\n", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("directory-session")
    session_manager.add_permission_grant(
        "directory-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(target.resolve()),
        capabilities=["read", "external_path"],
    )
    _grant(session_manager, external, access="read")

    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/scratch/": FilesystemBackend(root_dir=scratch, virtual_mode=True),
        },
        session_id="directory-session",
        workspace_root=workspace,
    )
    file_tools = {
        item.name: item for item in VersionedPatchMiddleware(backend).tools
    }
    directory_tools = {
        item.name: item for item in ExternalDirectoryMiddleware(backend).tools
    }

    exact = file_tools["stage_external_artifact"].func(
        file_path=str(target.resolve()),
        runtime=_runtime("call-file", goal_id=None),
    )
    assert exact.status == "success"
    directory_conflict = directory_tools["stage_external_directory"].func(
        directory_path=str(external.resolve()),
        runtime=_runtime("call-directory-conflict", goal_id=None),
    )
    assert directory_conflict.status == "error"
    assert "authoritative writable draft conflict" in directory_conflict.content

    exact_lease = next(
        item
        for item in session_manager.list_external_artifact_leases("directory-session")
        if item["status"] == "staged"
    )
    exact_lease["status"] = "abandoned"
    session_manager.upsert_external_artifact_lease("directory-session", exact_lease)
    directory = directory_tools["stage_external_directory"].func(
        directory_path=str(external.resolve()),
        runtime=_runtime("call-directory", goal_id=None),
    )
    assert directory.status == "success"

    exact_conflict = file_tools["stage_external_artifact"].func(
        file_path=str(target.resolve()),
        runtime=_runtime("call-file-conflict", goal_id=None),
    )
    assert exact_conflict.status == "error"
    assert "authoritative writable draft conflict" in exact_conflict.content


def test_directory_commit_rejects_symlink_parent_without_writing_outside(
    tmp_path: Path,
) -> None:
    from graph.middlewares.external_directory import _apply_directory_plan

    root = tmp_path / "authorized"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    error, rollback = _apply_directory_plan(
        root,
        "lease-test",
        {"added": ["link/pwn.txt"], "modified": [], "deleted": []},
        {"link/pwn.txt": b"escaped"},
        {},
    )

    assert error is not None
    assert rollback is None
    assert not (outside / "pwn.txt").exists()


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


def test_session_directory_grant_survives_container_rebuild_but_stays_workspace_bound(
    tmp_path: Path,
) -> None:
    from graph.permission_policy import RunPermissionContext
    from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
    from harness.models import RunStatus

    external, _scratch, _tools, session_manager = _setup(tmp_path)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    target = external / "report.txt"
    target.write_text("before\n", encoding="utf-8")
    coordinator, first = _start_bound_run(
        session_manager,
        query_id="query-session-grant-1",
        backend_id="container:first",
    )
    first_state = session_manager.get_run_state("directory-session", first.run_id)
    first_bindings = RunPermissionContext.from_config_snapshot(
        first_state["config_snapshot"]
    ).grant_bindings()
    _grant(
        session_manager,
        external,
        access="read",
        run_id=first.run_id,
        scope="session",
        bindings=first_bindings,
    )
    grant = _grant(
        session_manager,
        external,
        access="write",
        run_id=first.run_id,
        scope="session",
        bindings=first_bindings,
    )

    assert grant["scope"] == "session"
    assert grant["stable_bindings"] == {
        "approval_mode": "strict",
        "backend_mode": "docker",
        "policy_epoch": 1,
            "policy_version": "tool-execution-v4",
        "workspace_id": "workspace:stable",
    }
    assert "backend_id" not in grant["stable_bindings"]
    coordinator.transition(first, RunStatus.COMPLETED)

    second_coordinator, second = _start_bound_run(
        session_manager,
        query_id="query-session-grant-2",
        backend_id="container:replacement",
    )
    assert session_manager.has_external_directory_permission(
        "directory-session", external, access="write", run_id=second.run_id
    )
    assert not session_manager.has_external_directory_permission(
        "directory-session", sibling, access="write", run_id=second.run_id
    )
    workspace = tmp_path / "workspace"
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    broker_backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="directory-session",
        run_id=second.run_id,
        query_id=second.query_id,
        workspace_root=workspace,
    )
    assert broker_backend.read(str(target)).error is None
    assert broker_backend.ls(str(external)).error is None
    assert broker_backend.glob("*.txt", path=str(external)).error is None
    assert broker_backend.grep("before", path=str(external)).error is None
    assert broker_backend.edit(str(target), "before", "after").error is None
    created = external / "created.txt"
    assert broker_backend.write(str(created), "created\n").error is None
    deleted = broker_backend.delete_external_file(
        str(created),
        expected_sha256="sha256:" + hashlib.sha256(b"created\n").hexdigest(),
    )
    assert deleted["status"] == "completed"
    assert not created.exists()
    second_coordinator.transition(second, RunStatus.COMPLETED)

    _third_coordinator, third = _start_bound_run(
        session_manager,
        query_id="query-session-grant-3",
        backend_id="container:replacement",
        workspace_id="workspace:different",
    )
    assert not session_manager.has_external_directory_permission(
        "directory-session", external, access="write", run_id=third.run_id
    )


def test_session_directory_grant_is_invalid_after_permission_policy_epoch_changes(
    tmp_path: Path,
) -> None:
    from graph.permission_policy import RunPermissionContext
    from harness.models import RunStatus

    external, _scratch, _tools, session_manager = _setup(tmp_path)
    coordinator, first = _start_bound_run(
        session_manager,
        query_id="query-policy-1",
        backend_id="container:first",
    )
    first_state = session_manager.get_run_state("directory-session", first.run_id)
    bindings = RunPermissionContext.from_config_snapshot(first_state["config_snapshot"]).grant_bindings()
    _grant(
        session_manager,
        external,
        access="read",
        run_id=first.run_id,
        scope="session",
        bindings=bindings,
    )
    coordinator.transition(first, RunStatus.COMPLETED)

    policy = session_manager.set_approval_mode_if_idle(
        "directory-session",
        "smart",
        expected_epoch=1,
    )
    assert policy["policy_epoch"] == 2
    assert not any(
        item["type"] == "external_directory_read"
        for item in session_manager.list_permission_grants("directory-session")
    )
    _second_coordinator, second = _start_bound_run(
        session_manager,
        query_id="query-policy-2",
        backend_id="container:first",
    )
    assert not session_manager.has_external_directory_permission(
        "directory-session", external, access="read", run_id=second.run_id
    )


def test_run_directory_grants_from_concurrent_runs_do_not_supersede_each_other(
    tmp_path: Path,
) -> None:
    external, _scratch, _tools, session_manager = _setup(tmp_path)
    first = _grant(session_manager, external, access="read", run_id="run-a")
    second = _grant(session_manager, external, access="read", run_id="run-b")

    session_manager.migrate_permission_grants("directory-session")
    active = {
        item["id"]: item
        for item in session_manager.list_permission_grants("directory-session")
    }
    assert set(active) == {first["id"], second["id"]}
    assert active[first["id"]]["semantic_key"] != active[second["id"]]["semantic_key"]
    assert session_manager.has_external_directory_permission(
        "directory-session", external, access="read", run_id="run-a"
    )
    assert session_manager.has_external_directory_permission(
        "directory-session", external, access="read", run_id="run-b"
    )


def test_session_grant_reapproval_never_reuses_a_superseded_duplicate(tmp_path: Path) -> None:
    external, _scratch, _tools, session_manager = _setup(tmp_path)
    data = session_manager._read_file("directory-session")
    data["permissions"]["grants"] = [
        {
            "id": "grant-old",
            "type": "external_directory_read",
            "scope": "session",
            "target_kind": "exact_directory",
            "target": str(external.resolve()),
            "capabilities": ["read", "recursive", "external_path"],
            "source": "user",
            "created_at": 1.0,
        },
        {
            "id": "grant-new",
            "type": "external_directory_read",
            "scope": "session",
            "target_kind": "exact_directory",
            "target": str(external.resolve()),
            "capabilities": ["external_path", "recursive", "read"],
            "source": "user",
            "created_at": 2.0,
        },
    ]
    session_manager._write_file("directory-session", data)
    assert session_manager.migrate_permission_grants("directory-session") == 1

    approved = _grant(
        session_manager,
        external,
        access="read",
        scope="session",
    )

    assert approved["id"] == "grant-new"
    assert not approved.get("superseded_at")
    assert [item["id"] for item in session_manager.list_permission_grants("directory-session")] == [
        "grant-new"
    ]


def test_directory_pending_deduplicates_only_while_request_is_active(tmp_path: Path) -> None:
    from graph.permission_resume import PermissionResumeRegistry

    async def exercise() -> None:
        registry = PermissionResumeRegistry()
        kwargs = {
            "session_id": "directory-session",
            "query_id": "query-1",
            "run_id": "run-1",
            "path": tmp_path.resolve(),
            "access": "read",
            "operation": "stage_external_directory",
        }
        first = registry.create_external_directory_request(
            tool_call_id="call-1",
            **kwargs,
        )
        duplicate = registry.create_external_directory_request(
            tool_call_id="call-2",
            **kwargs,
        )
        assert duplicate["id"] == first["id"]
        assert registry.resolve(first["id"], {"type": "reject"})

        replacement = registry.create_external_directory_request(
            tool_call_id="call-3",
            **kwargs,
        )
        assert replacement["id"] != first["id"]
        assert replacement["status"] == "pending"

    asyncio.run(exercise())


def test_concurrent_directory_requests_share_ui_semantic_key_across_runs(
    tmp_path: Path,
) -> None:
    from graph.permission_resume import PermissionResumeRegistry

    async def exercise() -> None:
        registry = PermissionResumeRegistry()
        common = {
            "session_id": "directory-session",
            "path": tmp_path.resolve(),
            "access": "read",
            "operation": "grep",
            "grant_bindings": {
                "backend_mode": "docker",
                "workspace_id": "workspace:stable",
                "policy_epoch": 1,
            },
        }
        first = registry.create_external_directory_request(
            query_id="query-1",
            run_id="run-1",
            tool_call_id="call-1",
            **common,
        )
        second = registry.create_external_directory_request(
            query_id="query-2",
            run_id="run-2",
            tool_call_id="call-2",
            **common,
        )

        assert first["id"] != second["id"]
        assert first["semantic_key"] == second["semantic_key"]
        assert registry.resolve(first["id"], {"type": "approve", "grant_id": "grant-1"})
        peers = registry.resolve_compatible_session_external_directories(
            session_id="directory-session",
            path=str(tmp_path.resolve()),
            access="read",
            capabilities=["read", "recursive", "external_path"],
            decision={"type": "approve", "grant_id": "grant-1"},
            grant_bindings=common["grant_bindings"],
            exclude_request_id=first["id"],
        )
        assert peers == [second["id"]]
        assert registry.get(second["id"])["status"] == "resolved"

    asyncio.run(exercise())


def test_user_supplied_external_directory_gets_host_file_broker_instructions(
    tmp_path: Path,
) -> None:
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
    assert "复制、移动、建目录直接使用 execute 中的标准 cp/mv/mkdir" in content
    assert "ls/glob/grep/read_file" in content
    assert "Kernel 仅是底层隔离实现" in content
    assert "内部 HostFileBroker 原子提交" in content
    assert "模型无需处理 Grant、lease、staged path 或 hash 编排" in content


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
    assert (
        "直接对原始绝对路径使用 "
        "read_file/write_file/materialize_source_ref/patch_file"
    ) in content
    assert "若确认必须发现同目录依赖" in content
    assert "对直接父目录调用 ls/glob/grep" in content
    assert "精确写入由 HostFileBroker 原子落到正式路径" in content
    assert "不得猜测兄弟路径或提升到更高祖先目录" in content


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
    monkeypatch.setattr(
        session_manager,
        "get_run_state",
        lambda _session_id, _run_id: {
            "config_snapshot": {
                "permissions": {
                    "approval_mode": "strict",
                    "policy_epoch": 1,
                    "policy_version": "tool-execution-v3",
                },
                "execution": {
                    "backend_mode": "docker",
                    "backend_id": "container:ephemeral",
                    "workspace_id": "workspace:stable",
                },
            }
        },
    )
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
    assert "delete" in request["capabilities"]
    assert request["change_preview"]["新增文件"] == "new.txt"
    assert request["options"] == ["exact_directory_run", "exact_directory_session"]
    assert request["grant_bindings"]["workspace_id"] == "workspace:stable"
    permission_resume_registry._requests.pop(request["id"], None)
    permission_resume_registry._pending.pop(request["id"], None)


def test_permission_middleware_turns_external_grep_into_directory_hitl_even_with_broad_file_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    session_manager.initialize(state_dir)
    session_manager.create_session("directory-search-session")
    external = tmp_path / "external"
    external.mkdir()
    session_manager.add_permission_grant(
        "directory-search-session",
        grant_type="external_file_read",
        target_kind="all_external_files",
        target="*",
        capabilities=["read", "external_path"],
        scope="session",
    )
    captured: dict = {}

    def fake_interrupt(payload):
        captured.update(payload)
        return {"decisions": [{"type": "reject"}]}

    monkeypatch.setattr(permission_middleware_module, "interrupt", fake_interrupt)
    monkeypatch.setattr(
        session_manager,
        "get_run_state",
        lambda _session_id, _run_id: {
            "config_snapshot": {
                "permissions": {
                    "approval_mode": "strict",
                    "policy_epoch": 1,
                    "policy_version": "tool-execution-v3",
                },
                "execution": {
                    "backend_mode": "docker",
                    "backend_id": "container:ephemeral",
                    "workspace_id": "workspace:stable",
                },
            }
        },
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "grep",
                        "args": {"path": str(external), "pattern": "heatmapByYear"},
                        "id": "call-directory-grep",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "directory-search-session",
            "query_id": "query-search",
            "run_id": "run-search",
            "workspace_path": str(tmp_path / "workspace"),
        }
    )

    async def invoke():
        return ExternalFilePermissionMiddleware().after_model(state, runtime)

    assert asyncio.run(invoke()) is None
    request = captured["request"]
    assert request["type"] == "external_directory_read"
    assert request["operation"] == "grep"
    assert request["path"] == str(external.resolve())
    assert request["options"][0] == "exact_directory_session"
    assert "HostFileBroker" in request["change_preview"]["安全说明"]
    assert "不会授予 shell 访问" in request["change_preview"]["安全说明"]
    permission_resume_registry._requests.pop(request["id"], None)
    permission_resume_registry._pending.pop(request["id"], None)


@pytest.mark.asyncio
async def test_exact_file_sibling_discovery_requests_only_direct_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    state_dir = tmp_path / "state"
    external = tmp_path / "external"
    state_dir.mkdir()
    external.mkdir()
    target = external / "report.html"
    target.write_text("<script src='charts.js'></script>", encoding="utf-8")
    session_manager.initialize(state_dir)
    session_manager.create_session("sibling-session")
    session_manager.add_permission_grant(
        "sibling-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(target.resolve()),
        capabilities=["read", "external_path"],
        scope="session",
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
                        "name": "grep",
                        "args": {"path": str(external), "pattern": "charts.js"},
                        "id": "call-sibling-grep",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "sibling-session",
            "query_id": "query-sibling",
            "run_id": "run-sibling",
            "workspace_path": str(tmp_path / "workspace"),
        }
    )

    assert ExternalFilePermissionMiddleware().after_model(state, runtime) is None
    request = captured["request"]
    assert request["path"] == str(target.parent.resolve())
    assert request["target_kind"] == "exact_directory"
    assert request["options"][0] == "exact_directory_session"
    permission_resume_registry._requests.pop(request["id"], None)
    permission_resume_registry._pending.pop(request["id"], None)


def test_exact_file_sibling_discovery_never_prompts_for_broad_ancestor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.session_manager import session_manager

    state_dir = tmp_path / "state"
    external = tmp_path / "external"
    state_dir.mkdir()
    external.mkdir()
    target = external / "report.html"
    target.write_text("report", encoding="utf-8")
    session_manager.initialize(state_dir)
    session_manager.create_session("broad-sibling-session")
    session_manager.add_permission_grant(
        "broad-sibling-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(target.resolve()),
        capabilities=["read", "external_path"],
        scope="session",
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        permission_middleware_module,
        "interrupt",
        lambda payload: calls.append(payload),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "grep",
                        "args": {"path": str(tmp_path), "pattern": "report"},
                        "id": "call-broad-grep",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "broad-sibling-session",
            "query_id": "query-broad",
            "run_id": "run-broad",
            "workspace_path": str(tmp_path / "workspace"),
        }
    )

    assert ExternalFilePermissionMiddleware().after_model(state, runtime) is None
    assert calls == []


def test_html_validator_reuses_exact_directory_read_grant_without_hitl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.messages import AIMessage

    import graph.permission_middleware as permission_middleware_module
    from graph.permission_middleware import ExternalFilePermissionMiddleware
    from graph.session_manager import session_manager

    state_dir = tmp_path / "state"
    external = tmp_path / "external"
    workspace = tmp_path / "workspace"
    for directory in (state_dir, external, workspace):
        directory.mkdir()
    report = external / "report.html"
    report.write_text("<!doctype html><title>Report</title>", encoding="utf-8")
    session_manager.initialize(state_dir)
    session_manager.create_session("html-validator-permission-session")
    from harness.models import RunRecord, RunStatus

    run = RunRecord(
        run_id="run-validate-html",
        query_id="query-validate-html",
        session_id="html-validator-permission-session",
        objective=f"验证 {report}",
        status=RunStatus.PREPARING,
    )
    session_manager.start_harness_run(
        "html-validator-permission-session",
        run.model_dump(mode="json"),
    )
    session_manager.transition_run_status(
        "html-validator-permission-session",
        run.run_id,
        RunStatus.RUNNING.value,
    )
    session_manager.add_permission_grant(
        "html-validator-permission-session",
        grant_type="external_directory_read",
        target_kind="exact_directory",
        target=str(external.resolve()),
        capabilities=["read", "recursive", "external_path"],
        scope="run",
        metadata={"run_id": run.run_id},
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        permission_middleware_module,
        "interrupt",
        lambda payload: calls.append(payload),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "validate_html_report",
                        "args": {
                            "html_file_path": str(report),
                            "timeout": 120,
                        },
                        "id": "call-validate-html",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }
    runtime = SimpleNamespace(
        context={
            "session_id": "html-validator-permission-session",
            "query_id": "query-validate-html",
            "run_id": run.run_id,
            "workspace_path": str(workspace),
        }
    )

    assert ExternalFilePermissionMiddleware().after_model(state, runtime) is None
    assert calls == []


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
            "scope": "run",
        },
    )

    assert response.status_code == 200
    grant = response.json()["grant"]
    assert grant["type"] == "external_directory_write"
    assert grant["scope"] == "run"
    assert grant["target_kind"] == "exact_directory"
    assert grant["metadata"]["run_id"] == "run-7"
    assert grant["metadata"]["requested_target_kind"] == "exact_file"
    assert "delete" not in grant["capabilities"]
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
            "scope": "run",
        },
    )
    assert broad_response.status_code == 200
    broad_grant = broad_response.json()["grant"]
    assert broad_grant["target_kind"] == "exact_directory"
    assert broad_grant["target"] == str(external.resolve())
    assert broad_grant["scope"] == "run"
    assert broad_grant["metadata"]["requested_target_kind"] == "all_external_files"
    loop.close()


def test_permission_api_persists_session_directory_scope_and_resolves_compatible_pending(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_policy import RunPermissionContext
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager
    from harness.models import RunStatus

    state = tmp_path / "state"
    external = tmp_path / "external"
    state.mkdir()
    external.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("directory-session")
    first_coordinator, first = _start_bound_run(
        session_manager,
        query_id="query-parallel-directory-1",
        backend_id="container:first",
    )
    first_state = session_manager.get_run_state("directory-session", first.run_id)
    first_bindings = RunPermissionContext.from_config_snapshot(
        first_state["config_snapshot"]
    ).grant_bindings()

    loop = asyncio.new_event_loop()
    request_ids = ["perm-req-directory-first", "perm-req-directory-second"]
    futures = [loop.create_future(), loop.create_future()]
    for request_id, future in zip(
        request_ids,
        futures,
        strict=True,
    ):
        permission_resume_registry._pending[request_id] = future
        permission_resume_registry._requests[request_id] = {
            "id": request_id,
            "type": "external_directory_read",
            "session_id": "directory-session",
            "query_id": first.query_id,
            "run_id": first.run_id,
            "tool_call_id": f"call-{request_id}",
            "path": str(external.resolve()),
            "target_kind": "exact_directory",
            "capabilities": ["read", "recursive", "external_path"],
            "grant_bindings": first_bindings,
            "status": "pending",
        }

    response = TestClient(app).post(
        "/api/sessions/directory-session/permissions/external-files",
        json={
            "target_kind": "exact_directory",
            "path": str(external),
            "permission_request_id": request_ids[0],
            "scope": "session",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    grant = payload["grant"]
    assert grant["scope"] == "session"
    assert grant["target"] == str(external.resolve())
    assert "backend_id" not in grant["stable_bindings"]
    assert payload["auto_resumed_permission_request_ids"] == [request_ids[1]]
    assert futures[0].result()["grant_id"] == grant["id"]
    assert futures[1].result()["grant_id"] == grant["id"]
    assert session_manager.has_external_directory_permission(
        "directory-session", external, access="read", run_id=first.run_id
    )

    first_coordinator.transition(first, RunStatus.COMPLETED)
    loop.close()


def test_shell_directory_permission_api_persists_atomic_v3_grant_set(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from app import app
    from graph.permission_resume import permission_resume_registry
    from graph.session_manager import session_manager

    state = tmp_path / "state"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    for path in (state, source, destination):
        path.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("directory-session")
    _coordinator, run = _start_bound_run(
        session_manager,
        query_id="query-shell-api",
        backend_id="container:shell-api",
    )
    bindings = _active_shell_bindings(session_manager, run.run_id)
    request_id = "perm-req-shell-api"
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    permission_resume_registry._pending[request_id] = future
    permission_resume_registry._requests[request_id] = {
        "id": request_id,
        "type": "shell_directory_access",
        "authority_plane": "shell",
        "session_id": "directory-session",
        "query_id": run.query_id,
        "run_id": run.run_id,
        "tool_call_id": "call-shell-api",
        "grant_bindings": bindings,
        "grant_specs": [
            {"target": str(source), "access": "read", "delete": False},
            {"target": str(destination), "access": "read", "delete": False},
            {"target": str(destination), "access": "write", "delete": False},
        ],
        "status": "pending",
    }

    response = TestClient(app).post(
        "/api/sessions/directory-session/permissions/shell-directories",
        json={"permission_request_id": request_id, "scope": "run"},
    )

    assert response.status_code == 200
    grants = response.json()["grants"]
    assert len(grants) == 3
    assert {grant["binding_schema_version"] for grant in grants} == {3}
    assert {grant["metadata"]["authority_plane"] for grant in grants} == {"shell"}
    assert future.result()["grant_ids"] == [grant["id"] for grant in grants]
    active, revision = session_manager.permission_grants_snapshot("directory-session")
    assert len(active) == 3
    assert revision == 1
    listed = TestClient(app).get("/api/sessions/directory-session/permissions")
    assert listed.status_code == 200
    assert {
        grant["binding_schema_version"] for grant in listed.json()["grants"]
    } == {3}
    assert {
        grant["id"] for grant in listed.json()["grants"]
    } == {grant["id"] for grant in grants}
    loop.close()
