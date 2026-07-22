from __future__ import annotations

from pathlib import Path

from deepagents.backends import FilesystemBackend

from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
from graph.session_manager import session_manager
from harness.coordinators import HarnessRunCoordinator
from harness.models import RunStatus


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
            capabilities=[access, "recursive", "external_path"],
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


def test_authorized_directory_rejects_symlink_escape(tmp_path: Path) -> None:
    backend, external, outside, _run = _setup(tmp_path)
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = external / "escape.txt"
    link.symlink_to(secret)

    assert backend.can_access_external_path(str(link), access="read") is False
    assert backend.can_access_external_path(str(link), access="write") is False

