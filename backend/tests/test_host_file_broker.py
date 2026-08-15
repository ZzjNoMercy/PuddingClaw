from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.messages import ToolMessage

from graph.host_file_broker import HostFileBroker
from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
from graph.session_manager import session_manager
from harness.coordinators import HarnessRunCoordinator
from harness.models import RunStatus
from harness.verification_activations import build_verification_activations


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _setup(tmp_path: Path):
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    outside = tmp_path / "outside"
    for directory in (state, workspace, external, outside):
        directory.mkdir()
    session_manager.initialize(state)
    session_manager.create_session("broker-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="broker-session",
        query_id="broker-query",
        objective="edit the authorized external directory",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    for access in ("read", "write"):
        session_manager.add_permission_grant(
            "broker-session",
            grant_type=f"external_directory_{access}",
            target_kind="exact_directory",
            target=str(external.resolve()),
            capabilities=[
                access,
                *(["delete"] if access == "write" else []),
                "recursive",
                "external_path",
            ],
            scope="run",
            metadata={"run_id": run.run_id},
        )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="broker-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )
    return backend, external, outside, run


def test_atomic_replace_keeps_validation_and_commit_on_same_parent_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "authority"
    nested = authority_root / "nested"
    moved = authority_root / "moved"
    nested.mkdir(parents=True)
    target_path = nested / "target.txt"
    target_path.write_text("before", encoding="utf-8")
    target = HostFileBroker._authorized_path(
        canonical_path=target_path,
        authority_root=authority_root,
        grant_id="test",
        access="write",
    )
    assert target is not None
    original_reader = HostFileBroker._read_leaf_bytes
    swapped = False

    def swap_parent_after_first_read(
        directory_fd: int,
        leaf: str,
        *,
        canonical_path: Path,
    ) -> bytes | None:
        nonlocal swapped
        content = original_reader(
            directory_fd,
            leaf,
            canonical_path=canonical_path,
        )
        if not swapped:
            swapped = True
            nested.rename(moved)
            nested.mkdir()
            (nested / "target.txt").write_text("attacker", encoding="utf-8")
        return content

    monkeypatch.setattr(
        HostFileBroker,
        "_read_leaf_bytes",
        staticmethod(swap_parent_after_first_read),
    )

    HostFileBroker._atomic_replace(
        target,
        b"after",
        expected_before=b"before",
    )

    assert (moved / "target.txt").read_text(encoding="utf-8") == "after"
    assert (nested / "target.txt").read_text(encoding="utf-8") == "attacker"


def test_authorized_directory_uses_direct_host_file_tools_and_receipts(tmp_path: Path) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    target = external / "report.html"
    target.write_text("<option>2024</option>\n", encoding="utf-8")

    read = backend.read(str(target))
    assert read.error is None
    assert "2024" in str((read.file_data or {}).get("content") or "")

    listing = backend.ls(str(external))
    assert listing.error is None
    assert {item["path"] for item in listing.entries or []} == {str(target)}

    matches = backend.grep("2024", path=str(external), glob="*.html")
    assert matches.error is None
    assert [item["path"] for item in matches.matches or []] == [str(target)]

    edited = backend.edit(str(target), "2024", "2026")
    assert edited.error is None
    assert not {
        "lease_id",
        "staged_path",
        "source_sha256",
        "draft_sha256",
        "expected_source_sha256",
    }.intersection(vars(edited))
    assert edited.path == str(target)
    assert target.read_text(encoding="utf-8") == "<option>2026</option>\n"

    created = backend.write(str(external / "notes.txt"), "verified\n")
    assert created.error is None
    assert (external / "notes.txt").read_text(encoding="utf-8") == "verified\n"

    receipts = session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    )
    assert [item["operation"] for item in receipts] == ["edit", "create"]
    assert all(item["atomic"] is True for item in receipts)
    assert all(item["permission_grant_id"] for item in receipts)
    assert all(item["kind"] == "external_mutation_completed" for item in receipts)
    assert all(item["before_version_token"] for item in receipts)
    assert all(item["after_version_token"] for item in receipts)


def test_copy_and_hash_guarded_replace_are_atomic(tmp_path: Path) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    source = external / "report.html"
    target = external / "report-v2.html"
    source.write_text("<html>v1</html>\n", encoding="utf-8")

    copied = backend.copy_external_file(str(source), str(target))

    assert copied["status"] == "completed"
    assert copied["source_sha256"] == copied["target_sha256"]
    assert target.read_bytes() == source.read_bytes()

    refused = backend.copy_external_file(str(source), str(target))
    assert refused["status"] == "conflict"
    assert refused["error_code"] == "target_already_exists"

    stale = backend.replace_external_file(
        str(target),
        b"<html>stale</html>\n",
        expected_sha256="sha256:" + "0" * 64,
    )
    assert stale["status"] == "conflict"
    assert target.read_text(encoding="utf-8") == "<html>v1</html>\n"

    replaced = backend.replace_external_file(
        str(target),
        b"<html>v2</html>\n",
        expected_sha256=copied["target_sha256"],
    )
    assert replaced["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "<html>v2</html>\n"
    assert [
        item["operation"]
        for item in session_manager.list_external_mutation_receipts(
            "broker-session",
            run_id=run.run_id,
        )
    ] == ["copy", "replace"]


def test_unrestricted_replace_still_uses_atomic_mutation_receipt(tmp_path: Path) -> None:
    backend, _external, outside, run = _setup(tmp_path)
    backend.filesystem_mode = "unrestricted"
    target = outside / "smart.txt"
    target.write_text("before\n", encoding="utf-8")

    replaced = backend.replace_external_file(
        str(target),
        b"after\n",
        expected_sha256=_digest("before\n"),
        operation="patch",
    )

    assert replaced["status"] == "completed"
    assert replaced["authority_kind"] == "external"
    assert replaced["mutation_receipt_id"].startswith("external-mutation-")
    assert target.read_text(encoding="utf-8") == "after\n"
    receipts = session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    )
    assert [item["operation"] for item in receipts] == ["patch"]
    assert receipts[0]["atomic"] is True
    assert receipts[0]["permission_grant_id"] == "smart-unrestricted"


def test_workspace_replace_is_internal_and_never_requires_a_grant(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "report.html"
    target.write_text("<html>before</html>\n", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="",
        workspace_root=workspace,
    )

    replaced = backend.replace_external_file(
        "/workspace/report.html",
        b"<html>after</html>\n",
        expected_sha256=_digest("<html>before</html>\n"),
    )

    assert replaced["status"] == "completed"
    assert replaced["authority_kind"] == "workspace"
    assert "permission" not in json.dumps(replaced)
    assert target.read_text(encoding="utf-8") == "<html>after</html>\n"

    stale = backend.replace_external_file(
        "/workspace/report.html",
        b"<html>stale</html>\n",
        expected_sha256=_digest("<html>before</html>\n"),
    )
    assert stale["status"] == "conflict"
    assert target.read_text(encoding="utf-8") == "<html>after</html>\n"


def test_scratch_to_workspace_copy_never_requires_host_permission(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    (scratch / "report.html").write_text("<html>draft</html>\n", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    scratch_backend = FilesystemBackend(root_dir=scratch, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/scratch/": scratch_backend,
        },
        session_id="",
        workspace_root=workspace,
    )

    copied = backend.copy_external_file(
        "/scratch/report.html",
        "/workspace/report.html",
    )

    assert copied["status"] == "completed"
    assert copied["source_path"] == "/scratch/report.html"
    assert copied["target_path"] == "/workspace/report.html"
    assert copied["authority_kind"] == "virtual_workspace"
    assert "permission" not in json.dumps(copied)
    assert (workspace / "report.html").read_text(encoding="utf-8") == (
        "<html>draft</html>\n"
    )


def test_missing_scratch_route_fails_closed_instead_of_writing_to_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="",
        workspace_root=workspace,
    )

    result = backend.create_external_file(
        "/scratch/report.html",
        b"<html>must-not-land-in-workspace</html>\n",
    )

    assert result["status"] == "io_error"
    assert result["error_code"] == "internal_backend_create_unavailable"
    assert not (workspace / "scratch" / "report.html").exists()


def test_missing_scratch_source_route_cannot_read_workspace_fallback(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    disguised_scratch = workspace / "scratch"
    disguised_scratch.mkdir(parents=True)
    (disguised_scratch / "secret.txt").write_text(
        "wrong-authority\n",
        encoding="utf-8",
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="",
        workspace_root=workspace,
    )

    result = backend.copy_external_file(
        "/scratch/secret.txt",
        "/workspace/copied.txt",
    )

    assert result["status"] == "io_error"
    assert result["error_code"] == "internal_read_route_unavailable"
    assert not (workspace / "copied.txt").exists()


@pytest.mark.parametrize("path_style", ["virtual", "relative", "absolute"])
def test_workspace_alias_cannot_mutate_nested_managed_readonly_root(
    tmp_path: Path,
    path_style: str,
) -> None:
    workspace = tmp_path / "workspace"
    managed = workspace / "managed"
    managed.mkdir(parents=True)
    protected = managed / "protected.txt"
    protected.write_text("protected\n", encoding="utf-8")
    source = workspace / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="",
        managed_readonly_roots=(managed,),
        workspace_root=workspace,
    )
    def managed_path(name: str) -> str:
        if path_style == "virtual":
            return f"/workspace/managed/{name}"
        if path_style == "relative":
            return f"managed/{name}"
        return str((managed / name).resolve())

    protected_path = managed_path("protected.txt")

    results = [
        backend.replace_external_file(
            protected_path,
            b"replaced\n",
            expected_sha256=_digest("protected\n"),
        ),
        backend.create_external_file(
            managed_path("created.txt"),
            b"created\n",
        ),
        backend.copy_external_file(
            "/workspace/source.txt",
            managed_path("copied.txt"),
        ),
        backend.delete_external_file(
            protected_path,
            expected_sha256=_digest("protected\n"),
        ),
        backend.apply_external_file_transaction(
            [
                {
                    "file_path": protected_path,
                    "expected_sha256": _digest("protected\n"),
                    "content": "transaction\n",
                }
            ]
        ),
    ]

    assert all(result["status"] == "permission_required" for result in results)
    assert all(
        result["error_code"] == "managed_resource_read_only" for result in results
    )
    assert protected.read_text(encoding="utf-8") == "protected\n"
    assert not (managed / "created.txt").exists()
    assert not (managed / "copied.txt").exists()


def test_workspace_delete_is_internal_and_hash_guarded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "obsolete.txt"
    target.write_text("obsolete\n", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="",
        workspace_root=workspace,
    )

    refused = backend.delete_external_file(
        "/workspace/obsolete.txt",
        expected_sha256=_digest("stale\n"),
    )
    assert refused["status"] == "conflict"
    assert target.exists()

    deleted = backend.delete_external_file(
        "/workspace/obsolete.txt",
        expected_sha256=_digest("obsolete\n"),
    )
    assert deleted == {
        "status": "completed",
        "deleted_path": "/workspace/obsolete.txt",
        "authority_kind": "workspace",
    }
    assert not target.exists()


def test_workspace_transaction_is_internal_and_preflights_every_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("first-before\n", encoding="utf-8")
    second.write_text("second-before\n", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="",
        workspace_root=workspace,
    )

    conflict = backend.apply_external_file_transaction(
        [
            {
                "file_path": "/workspace/first.txt",
                "expected_sha256": _digest("first-before\n"),
                "content": "first-after\n",
            },
            {
                "file_path": "/workspace/second.txt",
                "expected_sha256": _digest("stale\n"),
                "content": "second-after\n",
            },
        ]
    )
    assert conflict["status"] == "conflict"
    assert first.read_text(encoding="utf-8") == "first-before\n"
    assert second.read_text(encoding="utf-8") == "second-before\n"

    completed = backend.apply_external_file_transaction(
        [
            {
                "file_path": "/workspace/first.txt",
                "expected_sha256": _digest("first-before\n"),
                "content": "first-after\n",
            },
            {
                "file_path": "/workspace/second.txt",
                "expected_sha256": _digest("second-before\n"),
                "content": "second-after\n",
            },
        ]
    )
    assert completed["status"] == "completed"
    assert completed["authority_kind"] == "internal"
    assert "permission" not in json.dumps(completed)
    assert first.read_text(encoding="utf-8") == "first-after\n"
    assert second.read_text(encoding="utf-8") == "second-after\n"


def test_transaction_cannot_mix_workspace_and_scratch_authority_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    workspace_file = workspace / "workspace.txt"
    scratch_file = scratch / "scratch.txt"
    workspace_file.write_text("workspace-before\n", encoding="utf-8")
    scratch_file.write_text("scratch-before\n", encoding="utf-8")
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    scratch_backend = FilesystemBackend(root_dir=scratch, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={
            "/workspace/": workspace_backend,
            "/scratch/": scratch_backend,
        },
        session_id="",
        workspace_root=workspace,
    )

    result = backend.apply_external_file_transaction(
        [
            {
                "file_path": "/workspace/workspace.txt",
                "expected_sha256": _digest("workspace-before\n"),
                "content": "workspace-after\n",
            },
            {
                "file_path": "/scratch/scratch.txt",
                "expected_sha256": _digest("scratch-before\n"),
                "content": "scratch-after\n",
            },
        ]
    )

    assert result["status"] == "io_error"
    assert result["error_code"] == "mixed_authority_transaction_unsupported"
    assert workspace_file.read_text(encoding="utf-8") == "workspace-before\n"
    assert scratch_file.read_text(encoding="utf-8") == "scratch-before\n"


def test_authorized_external_source_copies_to_workspace_without_external_write_grant(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    for directory in (state, workspace, external):
        directory.mkdir()
    source = external / "template.html"
    source.write_text("<html>template</html>\n", encoding="utf-8")

    session_manager.initialize(state)
    session_manager.create_session("workspace-copy-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="workspace-copy-session",
        query_id="workspace-copy-query",
        objective="复制外部模板到工作区",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.add_permission_grant(
        "workspace-copy-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(source.resolve()),
        capabilities=["read", "external_path"],
        scope="session",
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="workspace-copy-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )

    copied = backend.copy_external_file(
        str(source),
        "/workspace/report-v3.html",
    )

    assert copied == {
        "status": "completed",
        "source_path": str(source.resolve()),
        "source_sha256": _digest("<html>template</html>\n"),
        "target_path": "/workspace/report-v3.html",
        "target_sha256": _digest("<html>template</html>\n"),
        "authority_kind": "virtual_workspace",
    }
    assert (workspace / "report-v3.html").read_bytes() == source.read_bytes()
    assert session_manager.list_external_mutation_receipts(
        "workspace-copy-session",
        run_id=run.run_id,
    ) == []

    refused = backend.copy_external_file(
        str(source),
        "/workspace/report-v3.html",
    )
    assert refused["status"] == "conflict"
    assert refused["error_code"] == "target_already_exists"


def test_external_to_workspace_copy_still_requires_source_read_grant(
    tmp_path: Path,
) -> None:
    backend, _external, outside, _run = _setup(tmp_path)
    source = outside / "ungranted.html"
    source.write_text("<html>private</html>\n", encoding="utf-8")

    denied = backend.copy_external_file(
        str(source),
        "/workspace/private.html",
    )

    assert denied["status"] == "permission_required"
    assert denied["error_code"] == "source_read_permission_required"
    assert not (tmp_path / "workspace" / "private.html").exists()


def test_exact_file_write_grant_can_create_an_approved_missing_target(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    for directory in (state, workspace, external):
        directory.mkdir()
    source = external / "template.html"
    source.write_text("<html>template</html>\n", encoding="utf-8")
    target = external / "report-v3.html"

    session_manager.initialize(state)
    session_manager.create_session("missing-target-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="missing-target-session",
        query_id="missing-target-query",
        objective="复制模板创建 V3",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.add_permission_grant(
        "missing-target-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(source.resolve()),
        capabilities=["read", "external_path"],
    )
    write_grant = session_manager.add_permission_grant(
        "missing-target-session",
        grant_type="external_file_write",
        target_kind="exact_file",
        target=str(target.resolve()),
        capabilities=["write", "external_path"],
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="missing-target-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )

    copied = backend.copy_external_file(str(source), str(target))

    assert copied["status"] == "completed"
    assert target.read_bytes() == source.read_bytes()
    receipt = session_manager.list_external_mutation_receipts(
        "missing-target-session",
        run_id=run.run_id,
    )[-1]
    assert receipt["permission_grant_id"] == write_grant["id"]


def test_delete_then_create_emits_deprecation_warning(tmp_path: Path) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    target = external / "legacy-overwrite.txt"
    target.write_text("v1\n", encoding="utf-8")

    deleted = backend.delete_external_file(
        str(target),
        expected_sha256=_digest("v1\n"),
    )
    created = backend.create_external_file(
        str(target),
        b"v2\n",
    )

    assert deleted["status"] == "completed"
    assert created["status"] == "completed"
    assert created["warnings"]
    assert "overwrite_via_delete_deprecated" in created["warnings"][0]
    receipts = session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    )
    assert receipts[-1]["warnings"] == created["warnings"]


def test_declared_artifact_target_is_exact_write_authority_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    for directory in (state, workspace, external):
        directory.mkdir()
    source = external / "report.html"
    source.write_text("<html>v1</html>\n", encoding="utf-8")
    declared_target = external / "report-v2.html"
    undeclared_target = external / "other.html"

    session_manager.initialize(state)
    session_manager.create_session("declared-copy-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="declared-copy-session",
        query_id="declared-copy-query",
        objective=f"参考 {source}，创建新的 V2 版本 HTML",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.add_permission_grant(
        "declared-copy-session",
        grant_type="external_file_read",
        target_kind="exact_file",
        target=str(source.resolve()),
        capabilities=["read", "external_path"],
        scope="session",
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="declared-copy-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )

    copied = backend.copy_external_file(str(source), str(declared_target))
    denied = backend.copy_external_file(str(source), str(undeclared_target))

    assert copied["status"] == "completed"
    assert denied["status"] == "permission_required"
    assert not undeclared_target.exists()
    assert not session_manager.has_external_file_write_permission(
        "declared-copy-session",
        declared_target,
    )
    receipt = session_manager.list_external_mutation_receipts(
        "declared-copy-session",
        run_id=run.run_id,
    )[-1]
    assert receipt["permission_grant_id"] == f"declared-artifact:{run.run_id}"

    activations = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="copy-declared-html",
        tool_name="copy_file",
        args={
            "source_path": str(source.resolve()),
            "target_path": str(declared_target.resolve()),
        },
        result=ToolMessage(
            content=json.dumps(copied, ensure_ascii=False),
            tool_call_id="copy-declared-html",
            name="copy_file",
            status="success",
        ),
        session_id="declared-copy-session",
        workspace_path=str(workspace),
    )
    artifact_activation = next(item for item in activations if item.pack == "artifact")
    artifact_ref = next(
        ref
        for ref in artifact_activation.evidence_refs
        if ref.get("kind") == "artifact_write"
    )
    assert artifact_ref["authorized"] is True
    assert artifact_ref["authority_kind"] == "declared_artifact"
    assert artifact_ref["permission_grant_id"] == f"declared-artifact:{run.run_id}"
    assert artifact_ref["mutation_receipt_id"] == receipt["receipt_id"]

    from harness.deterministic_checks import _evaluate_artifact_delivery

    evaluation = _evaluate_artifact_delivery(
        "artifact_delivery",
        {
            "run_id": run.run_id,
            "workspace_path": str(workspace),
            "verification_activations": [
                item.model_dump(mode="json") for item in activations
            ],
            "declared_artifact_targets": [str(declared_target.resolve())],
            "permission_grants_authoritative": True,
            "active_permission_grant_ids": [],
            "final_content": f"已交付 {declared_target}",
            "evaluation_phase": "terminal",
        },
    )
    assert evaluation.passed is True


def test_read_only_directory_grant_allows_search_but_not_mutation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    for directory in (state, workspace, external):
        directory.mkdir()
    target = external / "report.txt"
    target.write_text("read only\n", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("readonly-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="readonly-session",
        query_id="readonly-query",
        objective="inspect without modifying",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.add_permission_grant(
        "readonly-session",
        grant_type="external_directory_read",
        target_kind="exact_directory",
        target=str(external.resolve()),
        capabilities=["read", "recursive", "external_path"],
        scope="run",
        metadata={"run_id": run.run_id},
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="readonly-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )

    assert backend.read(str(target)).error is None
    assert backend.ls(str(external)).error is None
    assert backend.glob("*.txt", path=str(external)).error is None
    assert backend.grep("read", path=str(external)).error is None
    assert backend.write(str(external / "new.txt"), "blocked\n").error is not None
    assert backend.edit(str(target), "read", "write").error is not None
    denied = backend.delete_external_file(
        str(target),
        expected_sha256=_digest("read only\n"),
    )
    assert denied["status"] == "permission_required"
    assert target.read_text(encoding="utf-8") == "read only\n"


def test_all_external_files_grant_allows_exact_file_grep_but_not_directory_search(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    for directory in (state, workspace, external):
        directory.mkdir()
    target = external / "report.txt"
    target.write_text("needle\n", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("broad-file-read-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="broad-file-read-session",
        query_id="broad-file-read-query",
        objective="search one known external file",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.add_permission_grant(
        "broad-file-read-session",
        grant_type="external_file_read",
        target_kind="all_external_files",
        target="*",
        capabilities=["read", "external_path"],
        scope="session",
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="broad-file-read-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )

    exact_file_result = backend.grep("needle", path=str(target))

    assert exact_file_result.error is None
    assert [item["path"] for item in exact_file_result.matches or []] == [str(target)]
    assert backend.can_access_external_path(str(target), access="read") is True
    assert backend.can_access_external_path(str(external), access="read") is False
    directory_result = backend.grep("needle", path=str(external))
    assert directory_result.error is not None
    assert "permission_required" in directory_result.error


def test_ungranted_external_absolute_path_never_falls_through_default_backend(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    for directory in (state, workspace, external):
        directory.mkdir()
    target = external / "secret.txt"
    target.write_text("secret\n", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("ungranted-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="ungranted-session",
        query_id="ungranted-query",
        objective="attempt ungranted access",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="ungranted-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )

    results = [
        backend.read(str(target)),
        backend.write(str(external / "new.txt"), "blocked\n"),
        backend.edit(str(target), "secret", "leaked"),
        backend.ls(str(external)),
        backend.glob("*.txt", path=str(external)),
        backend.grep("secret", path=str(external)),
    ]

    assert all(str(result.error or "").startswith("permission_required:") for result in results)
    assert target.read_text(encoding="utf-8") == "secret\n"
    assert not (external / "new.txt").exists()


def test_authorized_directory_rejects_symlink_escape(tmp_path: Path) -> None:
    backend, external, outside, _run = _setup(tmp_path)
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = external / "escape.txt"
    link.symlink_to(secret)

    assert backend.can_access_external_path(str(link), access="read") is False
    assert backend.can_access_external_path(str(link), access="write") is False


def test_authorized_directory_inode_is_bound_across_validation_and_commit(
    tmp_path: Path,
) -> None:
    backend, external, outside, _run = _setup(tmp_path)
    original = external.with_name("external-original")

    def swap_authority_root(_target, _content):
        external.rename(original)
        external.symlink_to(outside, target_is_directory=True)
        return {
            "status": "completed",
            "validation_receipt_id": "validation-toctou",
        }

    assert backend.host_file_broker is not None
    backend.host_file_broker.validation_runner = swap_authority_root

    result = backend.create_external_file(
        str(external / "escaped.html"),
        b"<html>must stay bounded</html>",
    )

    assert result["status"] == "io_error"
    assert result["error_code"] == "atomic_create_failed"
    assert not (outside / "escaped.html").exists()
    assert not (original / "escaped.html").exists()


class _ValidationBackend:
    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        del timeout
        candidate = shlex.split(command)[-1]
        assert candidate.startswith("/scratch/")
        content = (self.scratch / candidate.removeprefix("/scratch/")).read_text(
            encoding="utf-8"
        )
        if "INVALID" in content:
            return ExecuteResponse(output="SyntaxError: invalid candidate", exit_code=1)
        return ExecuteResponse(output="syntax ok", exit_code=0)


class _ExternalDirectoryExecutionBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def execute_external_directory(
        self,
        directory_path: str,
        command: str,
        *,
        timeout: int,
        writable: bool = False,
    ) -> ExecuteResponse:
        self.calls.append((directory_path, command, timeout))
        if writable:
            Path(directory_path, "report-v2.js").write_text(
                "const version = 2;\n",
                encoding="utf-8",
            )
        return ExecuteResponse(output="validated read-only tree", exit_code=0)


def test_broker_validation_bridge_binds_formal_hash_and_blocks_bad_bytes(
    tmp_path: Path,
) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    backend.execution_backend = _ValidationBackend(scratch)
    backend.execution_scratch_host_path = str(scratch)
    target = external / "app.js"
    target.write_text("const value = 1;\n", encoding="utf-8")

    valid = backend.edit(str(target), "1", "2")
    assert valid.error is None
    assert target.read_text(encoding="utf-8") == "const value = 2;\n"
    receipt = session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    )[-1]
    validation = receipt["validation_receipt"]
    assert validation["status"] == "passed"
    assert validation["commit_authority"] is True
    assert validation["artifact_refs"] == [
        {
            "artifact_id": validation["artifact_refs"][0]["artifact_id"],
            "path": str(target),
            "content_sha256": receipt["after_sha256"],
        }
    ]
    assert not (scratch / "validation").exists()
    activations = build_verification_activations(
        run_id=run.run_id,
        query_id=run.query_id,
        tool_call_id="call-edit",
        tool_name="edit_file",
        args={"file_path": str(target), "old_string": "1", "new_string": "2"},
        result=ToolMessage(
            content=f"Updated {target}",
            tool_call_id="call-edit",
            name="edit_file",
            status="success",
        ),
        session_id="broker-session",
        workspace_path=str(tmp_path / "workspace"),
    )
    assert any(
        ref.get("validation_receipt_id") == validation["validation_receipt_id"]
        for activation in activations
        for ref in activation.evidence_refs
    )
    from harness.deterministic_checks import _evaluate_artifact_delivery

    artifact_evaluation = _evaluate_artifact_delivery(
        "artifact_delivery",
        {
            "run_id": run.run_id,
            "workspace_path": str(tmp_path / "workspace"),
            "verification_activations": [
                activation.model_dump(mode="json") for activation in activations
            ],
            "active_permission_grant_ids": [receipt["permission_grant_id"]],
            "permission_grants_authoritative": True,
            "final_content": f"Updated {target}",
            "evaluation_phase": "terminal",
        },
    )
    assert artifact_evaluation.passed is True

    invalid = backend.edit(str(target), "2", "INVALID")
    assert invalid.error is not None
    assert invalid.error.startswith("validation_failed:")
    assert target.read_text(encoding="utf-8") == "const value = 2;\n"
    assert len(
        session_manager.list_external_mutation_receipts(
            "broker-session",
            run_id=run.run_id,
        )
    ) == 1


def test_code_like_write_fails_closed_when_validator_is_unavailable(
    tmp_path: Path,
) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    target = external / "app.js"
    target.write_text("const value = 1;\n", encoding="utf-8")

    edited = backend.edit(str(target), "1", "INVALID")

    assert edited.error is not None
    assert edited.error.startswith("infrastructure_error:")
    assert "fix_candidate_content" not in edited.error
    assert "retry_validator" in edited.error
    assert target.read_text(encoding="utf-8") == "const value = 1;\n"
    assert session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    ) == []


@pytest.mark.parametrize(
    ("mode", "exit_code", "output"),
    [
        ("exit127", 127, "node: command not found"),
        ("timeout", 124, "timed out"),
        ("oom", 137, "Killed: out of memory"),
        ("exception", None, "backend exploded"),
    ],
)
def test_validator_control_failures_never_request_content_changes(
    tmp_path: Path,
    mode: str,
    exit_code: int | None,
    output: str,
) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = external / "app.js"
    original = "const value = 1;\n"
    target.write_text(original, encoding="utf-8")

    class FailingValidationBackend:
        def execute(self, command: str, *, timeout: int | None = None):
            del command, timeout
            if mode == "exception":
                raise RuntimeError(output)
            return ExecuteResponse(output=output, exit_code=exit_code)

    backend.execution_backend = FailingValidationBackend()
    backend.execution_scratch_host_path = str(scratch)
    result = backend.replace_external_file(
        str(target),
        b"const value = 2;\n",
        expected_sha256=_digest(original),
    )

    assert result["status"] == "infrastructure_error"
    assert result["validation_receipt"]["failure_class"] == "infrastructure_failure"
    assert result["validation_receipt"]["content_observed"] is False
    assert result["next_action"] == "retry_validator_or_report_infrastructure_error"
    assert "fix_candidate_content" not in json.dumps(result)
    assert target.read_text(encoding="utf-8") == original
    assert session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    ) == []


def test_external_directory_command_requires_root_grant_and_uses_ephemeral_backend(
    tmp_path: Path,
) -> None:
    backend, external, outside, _run = _setup(tmp_path)
    execution = _ExternalDirectoryExecutionBackend()
    backend.execution_backend = execution

    completed = backend.execute_external_directory_command(
        str(external),
        "node --check app.js",
        timeout=45,
    )
    denied = backend.execute_external_directory_command(
        str(outside),
        "ls",
        timeout=45,
    )

    assert completed == {
        "status": "completed",
        "directory_path": str(external),
        "read_only": True,
        "ephemeral": True,
        "exit_code": 0,
        "output": "validated read-only tree",
        "truncated": False,
    }
    assert execution.calls == [(str(external), "node --check app.js", 45)]
    assert denied["status"] == "permission_required"


def test_spawn_external_directory_read_does_not_require_host_file_grant(
    tmp_path: Path,
) -> None:
    backend, external, _outside, _run = _setup(tmp_path)
    execution = _ExternalDirectoryExecutionBackend()
    execution.mode = "spawn"
    backend.execution_backend = execution
    backend.host_file_broker = None

    result = backend.execute_external_directory_command(
        str(external),
        "rg --files .",
        timeout=45,
    )

    assert result["status"] == "completed"
    assert execution.calls == [(str(external), "rg --files .", 45)]


def test_spawn_builtin_file_tools_read_external_paths_without_grants(
    tmp_path: Path,
) -> None:
    backend, _external, outside, _run = _setup(tmp_path)
    backend.execution_mode = "spawn"
    document = outside / "document.txt"
    document.write_text("puddingclaw spawn host read\n", encoding="utf-8")

    read = backend.read(str(document))
    listing = backend.ls(str(outside))
    matches = backend.grep("spawn host", path=str(outside), glob="*.txt")

    assert read.error is None
    assert "spawn host read" in str((read.file_data or {}).get("content") or "")
    assert listing.error is None
    assert [entry["path"] for entry in listing.entries or []] == [str(document)]
    assert matches.error is None
    assert [match["path"] for match in matches.matches or []] == [str(document)]


def test_writable_external_directory_command_only_mutates_staged_draft(
    tmp_path: Path,
) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    (external / "report.js").write_text("const version = 1;\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    staged = scratch / "external-directories" / "directory-lease-write"
    staged.mkdir(parents=True)
    (staged / "report.js").write_text("const version = 1;\n", encoding="utf-8")
    backend.execution_scratch_host_path = str(scratch)
    backend.external_directory_writable_enabled = True
    execution = _ExternalDirectoryExecutionBackend()
    backend.execution_backend = execution
    session_manager.upsert_external_directory_lease(
        "broker-session",
        {
            "lease_id": "directory-lease-write",
            "session_id": "broker-session",
            "run_id": run.run_id,
            "query_id": run.query_id,
            "goal_id": "",
            "goal_revision": None,
            "directory_path": str(external),
            "staged_dir": "/scratch/external-directories/directory-lease-write",
            "status": "staged",
            "source_manifest": {
                "report.js": {
                    "sha256": _digest("const version = 1;\n"),
                    "size": len("const version = 1;\n"),
                }
            },
        },
    )

    result = backend.execute_external_directory_command(
        str(external),
        "cp report.js report-v2.js",
        timeout=45,
        mode="writable_draft",
        lease_id="directory-lease-write",
    )

    assert result["status"] == "completed"
    assert result["draft_plan_preview"] == {
        "added": ["report-v2.js"],
        "modified": [],
        "deleted": [],
    }
    assert result["next_action"] == "prepare_external_directory_commit"
    assert not (external / "report-v2.js").exists()
    assert (staged / "report-v2.js").exists()


def test_rewind_restores_only_current_run_when_hashes_still_match(tmp_path: Path) -> None:
    backend, external, _outside, _run = _setup(tmp_path)
    target = external / "report.txt"
    created = external / "notes.txt"
    target.write_text("before\n", encoding="utf-8")

    assert backend.edit(str(target), "before", "after").error is None
    assert backend.write(str(created), "created\n").error is None
    result = backend.rewind_external_file_changes()

    assert result["status"] == "completed"
    assert len(result["rewound_receipt_ids"]) == 2
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not created.exists()
    assert backend.rewind_external_file_changes()["status"] == "noop"


def test_rewind_refuses_to_overwrite_concurrent_host_change(tmp_path: Path) -> None:
    backend, external, _outside, _run = _setup(tmp_path)
    target = external / "report.txt"
    target.write_text("before\n", encoding="utf-8")
    assert backend.edit(str(target), "before", "after").error is None
    target.write_text("concurrent\n", encoding="utf-8")

    result = backend.rewind_external_file_changes()

    assert result["status"] == "conflict"
    assert result["error"].startswith("conflict:")
    assert target.read_text(encoding="utf-8") == "concurrent\n"


def test_delete_is_single_file_versioned_and_rewindable(tmp_path: Path) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    target = external / "obsolete.txt"
    target.write_text("remove me\n", encoding="utf-8")

    conflict = backend.delete_external_file(
        str(target),
        expected_sha256=_digest("stale\n"),
    )
    assert conflict["status"] == "conflict"
    assert target.exists()

    deleted = backend.delete_external_file(
        str(target),
        expected_sha256=_digest("remove me\n"),
    )
    assert deleted["status"] == "completed"
    assert not target.exists()
    receipt = session_manager.list_external_mutation_receipts(
        "broker-session",
        run_id=run.run_id,
    )[-1]
    assert receipt["operation"] == "delete"
    assert receipt["after_sha256"] == "deleted"
    assert receipt["rewindable"] is True

    rewound = backend.rewind_external_file_changes()
    assert rewound["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "remove me\n"


def test_exact_file_write_does_not_imply_delete(tmp_path: Path) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    for directory in (state, workspace, external):
        directory.mkdir()
    target = external / "keep.txt"
    target.write_text("keep\n", encoding="utf-8")
    session_manager.initialize(state)
    session_manager.create_session("exact-delete-session")
    coordinator = HarnessRunCoordinator(session_manager)
    run, _goal = coordinator.start_run(
        session_id="exact-delete-session",
        query_id="exact-delete-query",
        objective="try exact file delete",
        goal_mode=False,
        verification_enabled=False,
    )
    coordinator.transition(run, RunStatus.RUNNING)
    session_manager.add_permission_grant(
        "exact-delete-session",
        grant_type="external_file_write",
        target_kind="exact_file",
        target=str(target),
        capabilities=["write", "external_path"],
    )
    workspace_backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    backend = PermissionedCompositeBackend(
        default=workspace_backend,
        routes={"/workspace/": workspace_backend},
        session_id="exact-delete-session",
        run_id=run.run_id,
        query_id=run.query_id,
        workspace_root=workspace,
    )

    denied = backend.delete_external_file(
        str(target),
        expected_sha256=_digest("keep\n"),
    )
    assert denied["status"] == "permission_required"
    assert target.exists()

    session_manager.add_permission_grant(
        "exact-delete-session",
        grant_type="external_file_delete",
        target_kind="exact_file",
        target=str(target),
        capabilities=["delete", "external_path"],
    )
    allowed = backend.delete_external_file(
        str(target),
        expected_sha256=_digest("keep\n"),
    )
    assert allowed["status"] == "completed"
    assert not target.exists()


def test_multi_file_transaction_is_all_or_nothing_and_journaled(tmp_path: Path) -> None:
    backend, external, _outside, run = _setup(tmp_path)
    first = external / "first.txt"
    second = external / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")

    completed = backend.apply_external_file_transaction(
        [
            {
                "file_path": str(first),
                "expected_sha256": _digest("one\n"),
                "content": "ONE\n",
            },
            {
                "file_path": str(second),
                "expected_sha256": _digest("two\n"),
                "content": "TWO\n",
            },
        ]
    )
    assert completed["status"] == "completed"
    assert first.read_text(encoding="utf-8") == "ONE\n"
    assert second.read_text(encoding="utf-8") == "TWO\n"
    receipts = [
        item
        for item in session_manager.list_external_mutation_receipts(
            "broker-session",
            run_id=run.run_id,
        )
        if item.get("transaction_id") == completed["transaction_id"]
    ]
    assert len(receipts) == 2
    assert all(item["diff"] for item in receipts)

    conflict = backend.apply_external_file_transaction(
        [
            {
                "file_path": str(first),
                "expected_sha256": _digest("ONE\n"),
                "content": "changed-one\n",
            },
            {
                "file_path": str(second),
                "expected_sha256": _digest("stale\n"),
                "content": "changed-two\n",
            },
        ]
    )
    assert conflict["status"] == "conflict"
    assert first.read_text(encoding="utf-8") == "ONE\n"
    assert second.read_text(encoding="utf-8") == "TWO\n"


def test_multi_file_transaction_validates_every_candidate_before_commit(
    tmp_path: Path,
) -> None:
    backend, external, _outside, _run = _setup(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    backend.execution_backend = _ValidationBackend(scratch)
    backend.execution_scratch_host_path = str(scratch)
    first = external / "first.js"
    second = external / "second.js"
    first.write_text("const first = 1;\n", encoding="utf-8")
    second.write_text("const second = 2;\n", encoding="utf-8")
    result = backend.apply_external_file_transaction(
        [
            {
                "file_path": str(first),
                "expected_sha256": _digest("const first = 1;\n"),
                "content": "const first = 10;\n",
            },
            {
                "file_path": str(second),
                "expected_sha256": _digest("const second = 2;\n"),
                "content": "INVALID\n",
            },
        ]
    )

    assert result["status"] == "validation_failed"
    assert first.read_text(encoding="utf-8") == "const first = 1;\n"
    assert second.read_text(encoding="utf-8") == "const second = 2;\n"
